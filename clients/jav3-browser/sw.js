// Jav3 browser extension: the service worker. One outbound WebSocket to the
// Jav3 server (/api/browser/ws); every request is a verb from the closed list
// in lib/verbs.js, run only in tabs this extension opened itself, in its own
// unfocused window. The operator's hand on it: per-site consent, a
// notification with Cancel on every action, Pause, Disconnect.
import {
  VerbError, validate, hostOf, isDenied, siteDecision, describe,
  parseLoginLine, baseUrl, wsUrl, parseElementId, frameConsentNeeded, shouldAdopt,
} from './lib/verbs.js';
import { readPage, clickEl, typeEl, scrollToEl, scrollPage } from './lib/page.js';

const ASK_TIMEOUT_MS = 60000;
const LOAD_TIMEOUT_MS = 20000;
const PING_MS = 20000;
const ICON = 'icon.png';

const S = {
  ws: null, pinger: null, retry: null, backoff: 1000,
  denyHosts: [], current: null, asks: new Map(),
  adopted: [],         // { id, url, at } popups adopted, so an action can report them
};

// The last read's frame map for a tab, kept in session storage (not just in
// memory) so a click/type/scroll_to_element still resolves after the MV3
// service worker is torn down and restarted between calls.
const FRAMES_KEY = tabId => 'frames:' + tabId;
async function saveFrames(tabId, map) {
  await chrome.storage.session.set({ [FRAMES_KEY(tabId)]: map });
}
async function loadFrames(tabId) {
  const k = FRAMES_KEY(tabId);
  return (await chrome.storage.session.get({ [k]: [] }))[k] || [];
}
async function dropFrames(tabId) {
  await chrome.storage.session.remove(FRAMES_KEY(tabId));
}

const cfg = () => chrome.storage.local.get({
  address: '', token: '', name: '', paused: false, notify: true, sites: {},
});
const sess = () => chrome.storage.session.get({ tabs: [], windowId: null, groupId: null });
const setStatus = (status, error = '') => chrome.storage.session.set({ status, error });

// --- connection ---------------------------------------------------------------------

async function connect() {
  if (S.ws && S.ws.readyState <= 1) return;
  const c = await cfg();
  if (!c.token || !c.address) { await setStatus('unpaired'); return; }
  let ws;
  try { ws = new WebSocket(wsUrl(c.address)); } catch (e) { await setStatus('offline', String(e)); return; }
  S.ws = ws;
  await setStatus('connecting');
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'hello', token: c.token, v: 1,
      ua: navigator.userAgent.slice(0, 120), paused: c.paused }));
    clearInterval(S.pinger);
    S.pinger = setInterval(() => send({ type: 'ping' }), PING_MS);
  };
  ws.onmessage = ev => { let m; try { m = JSON.parse(ev.data); } catch { return; } onFrame(m); };
  ws.onclose = async ev => {
    clearInterval(S.pinger);
    if (S.ws === ws) S.ws = null;
    cancelCurrent('the connection closed', false);
    if (ev.code === 4401 || ev.code === 4403) {
      await setStatus('unpaired', 'the server refused this extension\'s token — pair it again');
      return;
    }
    await setStatus('offline', ev.code === 4001 ? 'stopped from Jav3 Settings' : '');
    clearTimeout(S.retry);
    S.retry = setTimeout(connect, S.backoff);
    S.backoff = Math.min(S.backoff * 2, 60000);
  };
}

function send(obj) {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
}

async function onFrame(m) {
  if (m.type === 'welcome') {
    S.backoff = 1000;
    const c = await cfg();
    S.denyHosts = [...(Array.isArray(m.deny_hosts) ? m.deny_hosts : []), hostOf(c.address)].filter(Boolean);
    await chrome.storage.local.set({ name: typeof m.name === 'string' ? m.name : c.name });
    await setStatus('connected');
  } else if (m.type === 'req' && typeof m.id === 'string') {
    handleReq(m);
  } else if (m.type === 'kill') {
    cancelCurrent('stopped from Jav3', false);
  }
}

// --- requests -----------------------------------------------------------------------

function reply(id, ok, rest) { send({ type: 'res', id, ok, ...rest }); }

async function handleReq(m) {
  const c = await cfg();
  if (c.paused) return reply(m.id, false, { err: 'the operator paused Jav3 in the browser', code: 'paused' });
  if (S.current) return reply(m.id, false, { err: 'another browser action is still running', code: 'busy' });
  let p;
  try { p = validate(m.verb, m.params, { denyHosts: S.denyHosts }); } catch (e) {
    return reply(m.id, false, { err: String(e.message || e), code: 'invalid' });
  }
  let cancel;
  const cancelled = new Promise((_, rej) => { cancel = why => rej(Object.assign(new Error(why), { cancelled: true })); });
  S.current = { id: m.id, cancel };
  try {
    const out = await Promise.race([run(m.verb, p, c), cancelled]);
    reply(m.id, true, out);
  } catch (e) {
    reply(m.id, false, { err: String(e.message || e).slice(0, 500),
      code: e.cancelled ? 'cancelled' : 'failed' });
  } finally {
    S.current = null;
    setTimeout(() => { if (!S.current) chrome.notifications.clear('jav3-act'); }, 4000);
  }
}

function cancelCurrent(why, byOperator) {
  if (S.current) S.current.cancel(why);
  if (byOperator) {
    chrome.storage.local.set({ paused: true });
    send({ type: 'state', paused: true });
    send({ type: 'event', kind: 'cancelled' });
  }
}

async function ownTab(tabId) {
  const s = await sess();
  if (!s.tabs.includes(tabId)) throw new VerbError(`tab ${tabId} is not one Jav3 opened; Jav3 never touches the operator's tabs`);
  try { return await chrome.tabs.get(tabId); } catch { throw new VerbError(`tab ${tabId} is closed`); }
}

async function allowed(url, c) {
  const host = hostOf(url);
  if (!host) throw new VerbError('that tab is not showing a web page');
  if (isDenied(host, S.denyHosts)) throw new VerbError('that is the Jav3 server itself; Jav3 never uses it');
  const d = siteDecision(host, c.sites);
  if (d === 'allow') return host;
  if (d === 'deny') throw new VerbError(`the operator blocked Jav3 on ${host}`);
  return askSite(host);
}

function askSite(host) {
  let a = S.asks.get(host);
  if (!a) {
    a = { waiters: [] };
    a.timer = setTimeout(() => answer(host, null), ASK_TIMEOUT_MS);
    S.asks.set(host, a);
    chrome.notifications.create('ask:' + host, {
      type: 'basic', iconUrl: ICON, title: 'Allow Jav3 on ' + host + '?',
      message: 'Jav3 wants to use ' + host + ' in its own window. Allow for this site, or deny.',
      buttons: [{ title: 'Allow' }, { title: 'Deny' }], requireInteraction: true, priority: 2,
    });
    publishAsks();
  }
  return new Promise((resolve, reject) => a.waiters.push({ resolve, reject }));
}

async function answer(host, allow) {
  const a = S.asks.get(host);
  if (!a) return;
  clearTimeout(a.timer);
  S.asks.delete(host);
  chrome.notifications.clear('ask:' + host);
  publishAsks();
  if (allow !== null) {
    const c = await cfg();
    await chrome.storage.local.set({ sites: { ...c.sites, [host]: allow ? 'allow' : 'deny' } });
    send({ type: 'event', kind: allow ? 'site_allowed' : 'site_denied', site: host });
  }
  for (const w of a.waiters) {
    if (allow) w.resolve(host);
    else w.reject(new VerbError(allow === null
      ? `the operator did not allow ${host} (no answer in ${ASK_TIMEOUT_MS / 1000} s)`
      : `the operator denied Jav3 on ${host}`));
  }
}

function publishAsks() {
  chrome.storage.session.set({ asks: [...S.asks.keys()] });
  chrome.action.setBadgeText({ text: S.asks.size ? '?' : '' });
}

function notifyAct(verb, host, c) {
  if (!c.notify) return;
  chrome.notifications.create('jav3-act', {
    type: 'basic', iconUrl: ICON, title: 'Jav3', message: describe(verb, host),
    buttons: [{ title: 'Cancel' }], priority: 0, silent: true,
  });
}

async function jav3Window() {
  const s = await sess();
  if (s.windowId != null) {
    try { await chrome.windows.get(s.windowId); return s.windowId; } catch { /* closed */ }
  }
  return null;
}

// Bring a tab into Jav3's session: its own window and tab group. Used both for
// tabs Jav3 opens itself and for popups a Jav3 tab spawns (window.open,
// target=_blank, OAuth choosers). If no Jav3 window exists yet the tab's own
// window becomes it; otherwise the tab is moved in from wherever it opened.
async function joinSession(tab) {
  const s = await sess();
  let winId = s.windowId;
  if (winId != null) { try { await chrome.windows.get(winId); } catch { winId = null; } }
  if (winId == null) winId = tab.windowId;
  if (tab.windowId !== winId) {
    try { await chrome.tabs.move(tab.id, { windowId: winId, index: -1 }); } catch { /* gone */ }
  }
  const tabs = [...new Set([...s.tabs, tab.id])];
  let groupId = s.groupId;
  try {
    groupId = await chrome.tabs.group(groupId != null
      ? { groupId, tabIds: [tab.id] }
      : { tabIds: [tab.id], createProperties: { windowId: winId } });
    await chrome.tabGroups.update(groupId, { title: 'Jav3', color: 'purple' });
  } catch { groupId = null; /* no tab groups in this Chromium, or the group is gone */ }
  await chrome.storage.session.set({ tabs, windowId: winId, groupId });
}

function waitLoad(tabId) {
  return new Promise(resolve => {
    const done = () => { chrome.tabs.onUpdated.removeListener(l); clearTimeout(t); resolve(); };
    const l = (id, info) => { if (id === tabId && info.status === 'complete') done(); };
    const t = setTimeout(done, LOAD_TIMEOUT_MS);
    chrome.tabs.onUpdated.addListener(l);
    chrome.tabs.get(tabId).then(tab => { if (tab.status === 'complete' && tab.url !== 'about:blank') done(); }, done);
  });
}

async function inject(tabId, func, args, frameId = 0) {
  const [r] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] }, func, args,
  });
  return r ? r.result : null;
}

const info = async tabId => {
  const t = await chrome.tabs.get(tabId);
  return { tab: tabId, url: t.url || '', title: t.title || '' };
};

// The frames of a tab, main frame first, only http(s) frames the extension may
// read; the Jav3 server's own frames are dropped so the agent can never read or
// drive its control plane through an iframe on a consented page.
async function tabFrames(tabId) {
  let frames = [];
  try { frames = (await chrome.webNavigation.getAllFrames({ tabId })) || []; }
  catch { frames = []; }
  return frames
    .filter(f => !f.errorOccurred)
    .filter(f => f.frameId === 0 || (hostOf(f.url) && !isDenied(hostOf(f.url), S.denyHosts)))
    .sort((a, b) => a.frameId - b.frameId);
}

// Read every frame of the tab and stitch the results. Element numbers are local
// to each frame; here they gain a frame index ("f2:5"). The frame map is kept
// so a later click/type/scroll_to_element can resolve an id to its frameId.
async function readAllFrames(tabId, maxChars, selector) {
  const frames = await tabFrames(tabId);
  const map = [];
  const elements = [];
  const frameOut = [];
  let index = 0;
  let title = '';
  let topText = '';
  let selectorFound = false;
  // Share the text budget: the main frame gets it; subframes add elements only.
  for (const f of frames) {
    let r;
    try { r = await inject(tabId, readPage, [f.frameId === 0 ? maxChars : 0, selector || ''], f.frameId); }
    catch { r = null; }
    if (!r) continue;
    const host = hostOf(r.url) || hostOf(f.url) || '';
    if (index === 0) { title = r.title || ''; topText = r.text || ''; }
    if (r.probed) selectorFound = true;
    map.push({ index, frameId: f.frameId, url: r.url || f.url || '', host });
    frameOut.push({ index, host, url: r.url || f.url || '' });
    for (const e of (r.elements || [])) {
      elements.push({ id: `f${index}:${e.n}`, tag: e.tag, type: e.type, role: e.role,
                      name: e.name, text: e.text, box: e.box, inView: e.inView, frame: index });
    }
    index += 1;
  }
  await saveFrames(tabId, map);
  const i = await info(tabId);
  return { data: { tab: tabId, url: i.url, title: title || i.title, text: topText,
                   elements, frames: frameOut }, selectorFound, count: elements.length };
}

// Resolve an element id ("f2:5") to the frame the last read of this tab put it
// in. Returns { frameId, n, host, url } or throws (the read is stale / gone).
async function resolveElement(tabId, elementId) {
  const { frame, n } = parseElementId(elementId);
  const map = await loadFrames(tabId);
  const f = map.find(m => m.index === frame);
  if (!f) throw new VerbError(`element ${elementId}: read tab ${tabId} again (its frames changed)`);
  return { frameId: f.frameId, n, host: f.host, url: f.url };
}

async function run(verb, p, c) {
  if (verb === 'list_tabs') {
    notifyAct(verb, '', c);
    const s = await sess();
    const tabs = [];
    for (const id of s.tabs) { try { tabs.push(await info(id)); } catch { /* gone */ } }
    return { data: { tabs } };
  }
  if (verb === 'open_tab') {
    const host = await allowed(p.url, c);
    notifyAct(verb, host, c);
    const since = Date.now();
    const prev = await focusedWindow();
    const win = await jav3Window();
    let tab;
    if (win == null) {
      const w = await chrome.windows.create({ url: 'about:blank', focused: false, width: 1280, height: 900 });
      tab = w.tabs[0];
    } else {
      tab = await chrome.tabs.create({ windowId: win, url: 'about:blank', active: true });
    }
    await joinSession(tab);
    await chrome.tabs.update(tab.id, { url: p.url });
    await giveFocusBack(prev, tab.windowId);
    await waitLoad(tab.id);
    await giveFocusBack(prev, tab.windowId);
    return { data: { ...(await afterLoad(tab.id, c)), opened: openedSince(since, tab.id) } };
  }
  const tab = await ownTab(p.tab);
  if (verb === 'close_tab') {
    notifyAct(verb, hostOf(tab.url) || '', c);
    await chrome.tabs.remove(p.tab);
    await dropFrames(p.tab);
    return { data: { tab: p.tab } };
  }
  if (verb === 'navigate') {
    const host = await allowed(p.url, c);
    notifyAct(verb, host, c);
    const since = Date.now();
    const prev = await focusedWindow();
    await chrome.tabs.update(p.tab, { url: p.url });
    await new Promise(r => setTimeout(r, 200));
    await giveFocusBack(prev, tab.windowId);
    await waitLoad(p.tab);
    await giveFocusBack(prev, tab.windowId);
    await dropFrames(p.tab);   // the old element ids are gone
    return { data: { ...(await afterLoad(p.tab, c)), opened: openedSince(since, p.tab) } };
  }
  const host = await allowed(tab.url, c);       // per-site consent on the CURRENT top site
  notifyAct(verb, host, c);
  if (verb === 'read_page') {
    // An all-frames read, optionally retried until a selector or an element
    // count appears (bounded by wait_ms <= 10 s). read is fine across every
    // frame of an already-consented top-level site.
    const started = Date.now();
    let out = await readAllFrames(p.tab, p.max_chars, p.selector);
    while (p.wait_ms && Date.now() - started < p.wait_ms) {
      const enough = (p.min_elements ? out.count >= p.min_elements : false) ||
        (p.selector ? out.selectorFound : false) ||
        (!p.min_elements && !p.selector);
      if (enough) break;
      await new Promise(r => setTimeout(r, 350));
      out = await readAllFrames(p.tab, p.max_chars, p.selector);
    }
    return { data: out.data };
  }
  if (verb === 'screenshot_tab') {
    const prev = await focusedWindow();
    await chrome.tabs.update(p.tab, { active: true });   // active in Jav3's window only
    await giveFocusBack(prev, tab.windowId);
    const url = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 70 });
    const blob = await (await fetch(url)).blob();
    const bmp = await createImageBitmap(blob);
    const img = { mime: 'image/jpeg', w: bmp.width, h: bmp.height, b64: url.split(',', 2)[1] };
    bmp.close();
    return { data: await info(p.tab), image: img };
  }
  if (verb === 'scroll') {
    const r = await inject(p.tab, scrollPage, [p.pages]);
    if (!r || !r.ok) throw new VerbError((r && r.err) || 'scroll failed');
    return { data: await info(p.tab), text: `scrolled to ${r.y} of ${r.max}` };
  }
  // element-bound verbs: click, type, scroll_to_element. Resolve the id to its
  // frame from the last read of this tab.
  const el = await resolveElement(p.tab, p.element);
  if ((verb === 'click' || verb === 'type') && frameConsentNeeded(host, el.host)) {
    // A cross-origin frame from a DIFFERENT registrable domain must be allowed
    // too before we act inside it — the same per-site consent as a tab.
    await allowed(el.url, c);
  }
  const since = Date.now();
  const prev = await focusedWindow();
  let r;
  if (verb === 'click') r = await inject(p.tab, clickEl, [el.n], el.frameId);
  else if (verb === 'type') r = await inject(p.tab, typeEl, [el.n, p.text, p.submit], el.frameId);
  else r = await inject(p.tab, scrollToEl, [el.n], el.frameId);
  if (!r || !r.ok) throw new VerbError((r && r.err) || `${verb} failed`);
  if (verb !== 'scroll_to_element') { await new Promise(res => setTimeout(res, 500)); await waitLoad(p.tab); }
  await giveFocusBack(prev, tab.windowId);   // a click may have opened a popup that grabbed focus
  const out = await info(p.tab);
  return { data: { ...out, opened: openedSince(since, p.tab),
                   ...(verb === 'scroll_to_element' ? { text: r.inView ? 'in view' : 'scrolled' } : {}) } };
}

// Popups a Jav3 tab spawned during an action, so it can report their tab ids.
function openedSince(since, excludeTab) {
  const seen = S.adopted.filter(a => a.at >= since && a.id !== excludeTab);
  return seen.map(a => ({ tab: a.id, url: a.url || '' }));
}

// Jav3 must never pull the operator away from what they're doing. Chrome on
// macOS (and Brave) can ignore `focused: false` and raise a window on
// create/navigate/activate, so every such step remembers the window the
// operator was in and hands focus straight back to it.
async function focusedWindow() {
  try {
    const w = await chrome.windows.getLastFocused();
    return w && w.focused ? w.id : null;
  } catch { return null; }
}

async function giveFocusBack(prevId, jav3WinId) {
  if (prevId == null || prevId === jav3WinId) return;
  try {
    const now = await chrome.windows.getLastFocused();
    if (now && now.id !== prevId && now.focused) await chrome.windows.update(prevId, { focused: true });
  } catch { /* the operator's window closed meanwhile: nothing to restore */ }
}

// After a load the page may have redirected. A redirect to the Jav3 server is
// closed at once; any other new site is asked about by the next action on it
// (every tab-bound verb checks the tab's CURRENT site first).
async function afterLoad(tabId, c) {
  const i = await info(tabId);
  const host = hostOf(i.url);
  if (host && isDenied(host, S.denyHosts)) {
    await chrome.tabs.remove(tabId);
    throw new VerbError('the page redirected to the Jav3 server; tab closed');
  }
  return i;
}

// --- UI hooks -------------------------------------------------------------------------

chrome.notifications.onButtonClicked.addListener((id, idx) => {
  if (id === 'jav3-act' && idx === 0) {
    cancelCurrent('cancelled by the operator', true);
    chrome.notifications.clear(id);
  } else if (id.startsWith('ask:')) {
    answer(id.slice(4), idx === 0);
  }
});
chrome.notifications.onClosed.addListener((id, byUser) => {
  if (byUser && id.startsWith('ask:')) answer(id.slice(4), false);
});

// A Jav3 tab opened a new tab/window (window.open, target=_blank, an OAuth
// account chooser): adopt it into Jav3's window and session so it is reachable
// and grouped. Tabs the operator opened (no opener, or an opener Jav3 does not
// own) are never touched.
chrome.tabs.onCreated.addListener(async tab => {
  try {
    if (tab.id == null) return;
    const s = await sess();
    if (!shouldAdopt(tab.openerTabId, s.tabs, tab.id)) return;
    const prev = await focusedWindow();
    await joinSession(tab);
    S.adopted.push({ id: tab.id, url: tab.url || tab.pendingUrl || '', at: Date.now() });
    if (S.adopted.length > 50) S.adopted.splice(0, S.adopted.length - 50);
    const win = await jav3Window();
    await giveFocusBack(prev, win);   // hand focus back to the operator's window
    send({ type: 'event', kind: 'popup_adopted', site: hostOf(tab.url || tab.pendingUrl || '') || '' });
  } catch { /* best effort; a failed adopt just leaves the tab where it opened */ }
});

chrome.tabs.onRemoved.addListener(async id => {
  const s = await sess();
  await dropFrames(id);
  S.adopted = S.adopted.filter(a => a.id !== id);
  if (s.tabs.includes(id)) await chrome.storage.session.set({ tabs: s.tabs.filter(t => t !== id) });
});
chrome.windows.onRemoved.addListener(async id => {
  const s = await sess();
  if (s.windowId === id) await chrome.storage.session.set({ windowId: null, groupId: null });
});

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return false;
  (async () => {
    switch (msg && msg.cmd) {
      case 'pair': return pair(msg.line, msg.name);
      case 'pause':
        await chrome.storage.local.set({ paused: true });
        cancelCurrent('paused by the operator', false);
        send({ type: 'state', paused: true });
        return { ok: true };
      case 'resume':
        await chrome.storage.local.set({ paused: false });
        send({ type: 'state', paused: false });
        return { ok: true };
      case 'answer': await answer(msg.site, !!msg.allow); return { ok: true };
      case 'disconnect': return disconnect();
      case 'reconnect': S.backoff = 1000; await connect(); return { ok: true };
      default: return { ok: false, error: 'unknown command' };
    }
  })().then(respond, e => respond({ ok: false, error: String(e.message || e) }));
  return true;
});

async function pair(line, name) {
  const { address, code } = parseLoginLine(line);
  const base = baseUrl(address);
  const r = await fetch(base + '/api/devices/login', {
    method: 'POST', credentials: 'omit', redirect: 'error',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, name: (name || '').trim().slice(0, 64) || 'browser',
      hostname: '', platform: 'browser', scope: 'browser' }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok || !body.token) throw new Error(body.detail || `pairing failed (${r.status})`);
  if (S.ws) S.ws.close();
  await chrome.storage.local.set({ address: base, token: body.token, name: body.name || '' });
  S.backoff = 1000;
  await connect();
  return { ok: true, name: body.name, plainHttp: base.startsWith('http://') };
}

async function disconnect() {
  const c = await cfg();
  cancelCurrent('disconnected by the operator', false);
  await chrome.storage.local.set({ token: '' });
  if (S.ws) S.ws.close();
  clearTimeout(S.retry);
  if (c.token && c.address) {
    try {
      await fetch(c.address + '/api/devices/self', { method: 'DELETE', credentials: 'omit',
        redirect: 'error', headers: { Authorization: 'Bearer ' + c.token } });
    } catch { /* the token is forgotten here either way */ }
  }
  await setStatus('unpaired');
  return { ok: true };
}

chrome.alarms.create('jav3-keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'jav3-keepalive') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

