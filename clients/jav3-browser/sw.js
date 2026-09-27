// Jav3 browser extension: the service worker. One outbound WebSocket to the
// Jav3 server (/api/browser/ws); every request is a verb from the closed list
// in lib/verbs.js, run only in tabs this extension opened itself, in its own
// unfocused window. The operator's hand on it: per-site consent, a
// notification with Cancel on every action, Pause, Disconnect.
import {
  VerbError, validate, hostOf, isDenied, siteDecision, describe,
  parseLoginLine, baseUrl, wsUrl,
} from './lib/verbs.js';
import { readPage, clickEl, typeEl, scrollPage } from './lib/page.js';

const ASK_TIMEOUT_MS = 60000;
const LOAD_TIMEOUT_MS = 20000;
const PING_MS = 20000;
const ICON = 'icon.png';

const S = {
  ws: null, pinger: null, retry: null, backoff: 1000,
  denyHosts: [], current: null, asks: new Map(),
};

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

async function adopt(tab) {
  const s = await sess();
  const tabs = [...new Set([...s.tabs, tab.id])];
  let groupId = s.groupId;
  try {
    groupId = await chrome.tabs.group(groupId != null
      ? { groupId, tabIds: [tab.id] }
      : { tabIds: [tab.id], createProperties: { windowId: tab.windowId } });
    await chrome.tabGroups.update(groupId, { title: 'Jav3', color: 'purple' });
  } catch { groupId = null; /* no tab groups in this Chromium, or the group is gone */ }
  await chrome.storage.session.set({ tabs, windowId: tab.windowId, groupId });
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

async function inject(tabId, func, args) {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return r ? r.result : null;
}

const info = async tabId => {
  const t = await chrome.tabs.get(tabId);
  return { tab: tabId, url: t.url || '', title: t.title || '' };
};

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
    const prev = await focusedWindow();
    const win = await jav3Window();
    let tab;
    if (win == null) {
      const w = await chrome.windows.create({ url: 'about:blank', focused: false, width: 1280, height: 900 });
      tab = w.tabs[0];
    } else {
      tab = await chrome.tabs.create({ windowId: win, url: 'about:blank', active: true });
    }
    await adopt(tab);
    await chrome.tabs.update(tab.id, { url: p.url });
    await giveFocusBack(prev, tab.windowId);
    await waitLoad(tab.id);
    await giveFocusBack(prev, tab.windowId);
    return { data: await afterLoad(tab.id, c) };
  }
  const tab = await ownTab(p.tab);
  if (verb === 'close_tab') {
    notifyAct(verb, hostOf(tab.url) || '', c);
    await chrome.tabs.remove(p.tab);
    return { data: { tab: p.tab } };
  }
  if (verb === 'navigate') {
    const host = await allowed(p.url, c);
    notifyAct(verb, host, c);
    const prev = await focusedWindow();
    await chrome.tabs.update(p.tab, { url: p.url });
    await new Promise(r => setTimeout(r, 200));
    await giveFocusBack(prev, tab.windowId);
    await waitLoad(p.tab);
    await giveFocusBack(prev, tab.windowId);
    return { data: await afterLoad(p.tab, c) };
  }
  const host = await allowed(tab.url, c);
  notifyAct(verb, host, c);
  if (verb === 'read_page') {
    const r = await inject(p.tab, readPage, [p.max_chars]);
    return { data: { tab: p.tab, ...r } };
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
  const func = { click: clickEl, type: typeEl, scroll: scrollPage }[verb];
  const args = { click: [p.element], type: [p.element, p.text, p.submit], scroll: [p.pages] }[verb];
  const r = await inject(p.tab, func, args);
  if (!r || !r.ok) throw new VerbError((r && r.err) || `${verb} failed`);
  if (verb !== 'scroll') { await new Promise(res => setTimeout(res, 500)); await waitLoad(p.tab); }
  return { data: await info(p.tab), text: verb === 'scroll' ? `scrolled to ${r.y} of ${r.max}` : undefined };
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

chrome.tabs.onRemoved.addListener(async id => {
  const s = await sess();
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

