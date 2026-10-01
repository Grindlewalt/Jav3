// node --test clients/jav3-browser/test/
// The service worker's click paths end to end, against a fake chrome: a request
// frame in, the reply frame out, and the debugger commands in between. Covers
// the wiring lib/trusted.js cannot (consent, popups, via fields, errors).
import test from 'node:test';
import assert from 'node:assert/strict';

const store = { local: {}, session: {} };
const area = o => ({
  get: async d => (typeof d === 'string' ? { [d]: o[d] } : Object.fromEntries(Object.entries(d || {}).map(([k, v]) => [k, k in o ? o[k] : v]))),
  set: async v => { Object.assign(o, v); },
  remove: async ks => { for (const k of [].concat(ks)) delete o[k]; },
});
const ev = () => { const f = []; return { addListener: x => f.push(x), removeListener: x => { const i = f.indexOf(x); if (i >= 0) f.splice(i, 1); }, fire: (...a) => f.slice().forEach(x => x(...a)) }; };

const pages = {};                        // frameId -> { fnName: result | fn }
const dbg = { calls: [], attachError: null, onSend: null };
const tabsState = { 7: { id: 7, url: 'https://deltamath.com/sign-in', title: 'Sign in', status: 'complete', active: true, windowId: 1 } };
const onCreated = ev();
const frames = [
  { frameId: 0, parentFrameId: -1, url: 'https://deltamath.com/sign-in' },
  { frameId: 5, parentFrameId: 0, url: 'https://accounts.google.com/gsi/button' },
];

globalThis.chrome = {
  runtime: { id: 'x', getManifest: () => ({ version: '0.6.0' }), onMessage: ev(), onStartup: ev(), onInstalled: ev() },
  storage: { local: area(store.local), session: area(store.session), onChanged: ev() },
  alarms: { create() {}, onAlarm: ev() },
  action: { setBadgeText() {} },
  notifications: { create() {}, clear() {}, onButtonClicked: ev(), onClosed: ev() },
  windows: { getLastFocused: async () => ({ id: 1, focused: true }), get: async id => ({ id }), update: async () => ({}), onRemoved: ev() },
  tabs: {
    get: async id => { if (!tabsState[id]) throw new Error('no tab'); return { ...tabsState[id] }; },
    update: async (id, p) => { Object.assign(tabsState[id], p); return tabsState[id]; },
    move: async () => ({}), group: async () => 1, remove: async () => {},
    onCreated, onRemoved: ev(), onUpdated: ev(),
  },
  tabGroups: { update: async () => ({}) },
  webNavigation: { getAllFrames: async () => frames },
  scripting: {
    executeScript: async ({ target, func, args }) => {
      if (!func) return [];
      const a = (pages[target.frameIds[0]] || {})[func.name];
      return [{ result: typeof a === 'function' ? a(...args) : a }];
    },
  },
  debugger: {
    attach: async t => { dbg.calls.push('attach'); if (dbg.attachError) throw new Error(dbg.attachError); },
    detach: async t => { dbg.calls.push('detach'); },
    sendCommand: async (t, method, p) => { dbg.calls.push(`${method}:${p.type}@${p.x},${p.y}`); if (dbg.onSend) dbg.onSend(p); return {}; },
    getTargets: async () => [], onDetach: ev(),
  },
};

const sockets = [];
globalThis.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
  send(s) { this.sent.push(JSON.parse(s)); }
  close() {}
};

store.local.address = 'http://jav3.lan:8000';
store.local.token = 'tok';
store.local.sites = { 'deltamath.com': 'allow', 'accounts.google.com': 'allow' };
store.session.tabs = [7];
store.session.windowId = 1;
store.session['frames:7'] = [
  { index: 0, frameId: 0, url: 'https://deltamath.com/sign-in', host: 'deltamath.com' },
  { index: 1, frameId: 5, url: 'https://accounts.google.com/gsi/button', host: 'accounts.google.com' },
];

await import('../sw.js');
await new Promise(r => setTimeout(r, 20));
const ws = sockets[0];
ws.onmessage({ data: JSON.stringify({ type: 'welcome', name: 'b', deny_hosts: ['jav3.lan'] }) });

let n = 0;
async function ask(verb, params) {
  const id = 'r' + (++n);
  ws.onmessage({ data: JSON.stringify({ type: 'req', id, verb, params }) });
  for (let i = 0; i < 200; i++) {
    const res = ws.sent.find(m => m.type === 'res' && m.id === id);
    if (res) return res;
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error('no reply to ' + verb);
}

function gsiPages(extra = {}) {
  pages[0] = {
    frameInfo: () => ({ vp: { w: 1280, h: 1200 }, self: null, iframes: [{ src: 'https://accounts.google.com/gsi/button', x: 800, y: 640, w: 200, h: 44 }] }),
    hitIframe: () => ({ ok: true }),
    pageSig: 'sig-a', domQuiet: { quiet: true, ms: 1 },
    probeAt: () => ({ ok: true, iframe: { src: 'https://accounts.google.com/gsi/button', x: 800, y: 640, w: 200, h: 44 } }),
    clickAt: { ok: false, code: 'frame', err: 'that point is inside an iframe, which a script-made click cannot reach by coordinates' },
    ...extra,
  };
  pages[5] = {
    measureEl: () => ({ ok: true, pt: { x: 80, y: 22 }, vp: { w: 200, h: 44 }, label: 'div "Sign in with Google"' }),
    frameInfo: () => ({ vp: { w: 200, h: 44 }, self: null, iframes: [] }),
    armClick: { ok: true },
    takeClick: { ok: true, lost: false, down: { trusted: true, hit: 'div "Sign in with Google"', onEl: true }, click: { onEl: true } },
    probeAt: () => ({ ok: true, hit: 'div "Sign in with Google"' }),
    clickEl: { ok: true, text: 'clicked div "Sign in with Google"' },
    pageSig: 'sig-a',
  };
}
const reset = () => { dbg.calls.length = 0; dbg.attachError = null; dbg.onSend = null; chrome.debugger.attach = chrome.debugger.attach; };

test('a click on an element in the Google sign-in iframe is real mouse input', async () => {
  gsiPages(); reset();
  const res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.data.via, 'trusted');
  assert.equal(typeof res.data.debug_ms, 'number');
  assert.equal(res.data.text, 'clicked div "Sign in with Google"');
  assert.deepEqual(dbg.calls, ['attach', 'Input.dispatchMouseEvent:mouseMoved@880,662',
    'Input.dispatchMouseEvent:mousePressed@880,662', 'Input.dispatchMouseEvent:mouseReleased@880,662', 'detach']);
  assert.deepEqual(res.data.opened, []);
});

test('a popup the click opened is adopted and listed with its address', async () => {
  gsiPages(); reset();
  tabsState[12] = { id: 12, url: '', pendingUrl: 'https://accounts.google.com/o/oauth2/auth?x=1', windowId: 2, status: 'loading' };
  dbg.onSend = p => { if (p.type === 'mouseReleased') onCreated.fire({ id: 12, openerTabId: 7, pendingUrl: 'https://accounts.google.com/o/oauth2/auth?x=1', windowId: 2 }); };
  const res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.equal(res.ok, true);
  assert.deepEqual(res.data.opened, [{ tab: 12, url: 'https://accounts.google.com/o/oauth2/auth?x=1' }]);
  const listed = await ask('list_tabs', {});
  assert.deepEqual(listed.data.tabs.map(t => t.tab).sort(), [12, 7]);
});

test('a popup that closed itself again is still reported', async () => {
  gsiPages(); reset();
  dbg.onSend = p => { if (p.type === 'mouseReleased') onCreated.fire({ id: 13, openerTabId: 7, pendingUrl: 'https://accounts.google.com/x', windowId: 2 }); };
  const res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.deepEqual(res.data.opened, [{ tab: 13, url: 'https://accounts.google.com/x', closed: true }]);
});

test('a click by coordinates inside the iframe is allowed and keeps typing aimed at that frame', async () => {
  gsiPages(); reset();
  const res = await ask('click', { tab: 7, x: 1280, y: 974.5 });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.deepEqual([res.data.via, res.data.text], ['trusted', 'clicked div "Sign in with Google" (inside the iframe from accounts.google.com)']);
  assert.ok(dbg.calls.includes('Input.dispatchMouseEvent:mousePressed@1280,974.5'));
  assert.equal(store.session['focus:7'], 5);
});

test('without the debugger permission the click is a script event and says why', async () => {
  gsiPages(); reset();
  const dbgApi = chrome.debugger;
  chrome.debugger = undefined;
  try {
    const res = await ask('click', { tab: 7, element: 'f1:1' });
    assert.equal(res.ok, true);
    assert.deepEqual([res.data.via, res.data.via_why], ['synthetic', 'no_permission']);
    assert.equal(res.data.text, 'clicked div "Sign in with Google"');
    const xy = await ask('click', { tab: 7, x: 1280, y: 974 });
    assert.deepEqual([xy.ok, xy.code, xy.via, xy.via_why], [false, 'frame', 'synthetic', 'no_permission']);
  } finally { chrome.debugger = dbgApi; }
});

test('switched off in Options, or refused by Chrome, the click falls back with the reason', async () => {
  gsiPages(); reset();
  store.local.trusted = false;
  let res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.deepEqual([res.ok, res.data.via, res.data.via_why], [true, 'synthetic', 'disabled']);
  assert.deepEqual(dbg.calls, []);
  store.local.trusted = true;
  dbg.attachError = 'Another debugger is already attached to the tab with id: 7.';
  res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.deepEqual([res.ok, res.data.via, res.data.via_why], [true, 'synthetic', 'attach_failed']);
  assert.match(res.data.via_detail, /Another debugger/);
});

test('covered and stale elements are refused with the old messages, and no mouse event', async () => {
  gsiPages(); reset();
  pages[5].measureEl = () => ({ ok: true, pt: { x: 1, y: 1 }, vp: { w: 9, h: 9 }, label: 'x', covered: { n: 4, name: 'Accept cookies' } });
  let res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.deepEqual([res.ok, res.code], [false, 'covered']);
  assert.match(res.err, /covered by another element \("Accept cookies"\) — dismiss it first or click the covering element f1:4/);
  pages[5].measureEl = { ok: false, code: 'stale' };
  res = await ask('click', { tab: 7, element: 'f1:1' });
  assert.deepEqual([res.ok, res.code, res.err], [false, 'stale', 'element is no longer on the page']);
  assert.ok(!dbg.calls.some(c => c.includes('mousePressed')));
});

test('a different site in the iframe asks the operator first, and a denial stops the click', async () => {
  gsiPages(); reset();
  store.local.sites = { 'deltamath.com': 'allow', 'accounts.google.com': 'deny' };
  const res = await ask('click', { tab: 7, x: 1280, y: 974 });
  assert.deepEqual([res.ok, res.code], [false, 'failed']);
  assert.match(res.err, /the operator blocked Jav3 on accounts\.google\.com/);
  assert.deepEqual(dbg.calls, []);
  store.local.sites = { 'deltamath.com': 'allow', 'accounts.google.com': 'allow' };
});

test('the debugger is never attached to a tab Jav3 did not open', async () => {
  gsiPages(); reset();
  tabsState[99] = { id: 99, url: 'https://deltamath.com/', status: 'complete', active: true, windowId: 1 };
  const res = await ask('click', { tab: 99, element: 'f0:1' });
  assert.equal(res.ok, false);
  assert.deepEqual(dbg.calls, []);
});
