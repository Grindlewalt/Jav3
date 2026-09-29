// The closed verb list, extension side. Pure (no chrome.* calls) so node can
// test it: node --test clients/jav3-browser/test/
// Mirrors backend/browser.py `validate`; both sides check every request.
import './dom.js';

const DOM = globalThis.__jav3Dom;

export const VERBS = Object.freeze({
  open_tab: 'read', navigate: 'read', read_page: 'read', scroll: 'read',
  scroll_to_element: 'read', screenshot_tab: 'read', close_tab: 'read',
  list_tabs: 'read', back: 'read', forward: 'read',
  click: 'act', type: 'act', select: 'act', hover: 'act', key: 'act',
});
const TAB_VERBS = new Set(Object.keys(VERBS).filter(v => v !== 'open_tab' && v !== 'list_tabs'));
// Verbs whose `element` id comes from a browser_read_page of that tab: they
// need a fresh all-frames read (enforced host-side, backend/browser.py).
export const ELEMENT_VERBS = Object.freeze(['click', 'type', 'scroll_to_element', 'select', 'hover']);
export const TEXT_CAP = 2000;
export const URL_CAP = 2000;
export const PAGE_TEXT_CAP = 20000;
export const WAIT_CAP_MS = 10000;         // bounded retry budget on read_page
export const MAX_FRAME_INDEX = 999;
export const MAX_ELEMENT_N = 100000;
export const OPTION_CAP = 500;
// read_page: auto = candidates when < 8 interactive elements are in view
export const READ_MODES = Object.freeze(['auto', 'all', 'interactive']);

export class VerbError extends Error {
  constructor(msg, code) { super(msg); if (code) this.code = code; }
}

// "Ctrl+Shift+t" -> "ctrl+shift+t"; the desk's grammar (lib/dom.js). Throws VerbError.
export function normalizeCombo(combo) {
  try { return DOM.normalizeCombo(combo); } catch (e) { throw new VerbError(e.message); }
}

function int(params, k, lo, hi, dflt) {
  const v = params[k] === undefined ? dflt : params[k];
  if (typeof v !== 'number' || !Number.isInteger(v)) throw new VerbError(`${k} must be a whole number`);
  if (v < lo || v > hi) throw new VerbError(`${k}=${v} is outside ${lo}..${hi}`);
  return v;
}

function coord(params, k) {
  const v = params[k];
  if (typeof v !== 'number' || !Number.isFinite(v)) throw new VerbError(`${k} must be a number`);
  if (v < 0 || v > 100000) throw new VerbError(`${k}=${v} is outside 0..100000`);
  return v;
}

// Screenshot pixels per CSS px: captureVisibleTab returns the viewport at
// device pixels, so image width / viewport width (devicePixelRatio x zoom);
// the reported devicePixelRatio when the viewport is unknown. The server
// divides a screenshot point by this to get the CSS px clickAt wants.
export function shotScale(imgW, imgH, viewport) {
  const vw = viewport && Number(viewport.w), vh = viewport && Number(viewport.h);
  const dpr = viewport && Number(viewport.dpr) > 0 ? Number(viewport.dpr) : 1;
  const x = imgW > 0 && vw > 0 ? imgW / vw : dpr;
  const y = imgH > 0 && vh > 0 ? imgH / vh : x;
  return { x, y };
}

// A screenshot point -> CSS px of the viewport (what the server does before
// sending x, y; mirrored here so both sides agree and node can test it).
export function toCssPoint(x, y, scale) {
  return { x: Math.round((x / scale.x) * 10) / 10, y: Math.round((y / scale.y) * 10) / 10 };
}

export function hostOf(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.hostname.toLowerCase().replace(/^\[|\]$/g, '') || null;
  } catch { return null; }
}

export function checkUrl(url, denyHosts = []) {
  if (typeof url !== 'string' || !url.trim()) throw new VerbError('url is required');
  url = url.trim();
  if (url.length > URL_CAP || !/^https?:\/\/\S+$/i.test(url)) {
    throw new VerbError(`only http(s) URLs up to ${URL_CAP} characters can be opened`);
  }
  let u;
  try { u = new URL(url); } catch { throw new VerbError('that URL does not parse'); }
  if (u.username || u.password) throw new VerbError('URLs with a user:password@ part are refused');
  const host = hostOf(url);
  if (!host) throw new VerbError('that URL has no host');
  if (isDeniedUrl(url, denyHosts)) throw new VerbError('that is the Jav3 server itself; Jav3 never opens it');
  return url;
}

// Normalise a host for the deny compare: lowercase, no brackets, no trailing
// dot, IPv4-mapped IPv6 ("::ffff:127.0.0.1") as the plain IPv4.
export function normHost(h) {
  h = String(h || '').trim().toLowerCase().replace(/^\[|\]$/g, '').replace(/\.+$/, '');
  const m = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/.exec(h);
  if (m) return m[1];
  const w = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(h);
  if (w) {
    const a = parseInt(w[1], 16), b = parseInt(w[2], 16);
    return `${a >> 8}.${a & 255}.${b >> 8}.${b & 255}`;
  }
  return h;
}

function isLoopback(h) { return h === '::1' || /^127\.\d+\.\d+\.\d+$/.test(h); }

// "host", "host:port", "[v6]:port" or a bare v6 -> { host, port|null }
function splitEntry(e) {
  e = String(e || '').trim().toLowerCase();
  let m = /^\[([^\]]+)\](?::(\d+))?$/.exec(e);
  if (m) return { host: normHost(m[1]), port: m[2] ? Number(m[2]) : null };
  if ((e.match(/:/g) || []).length > 1) return { host: normHost(e), port: null };
  m = /^(.*?)(?::(\d+))?$/.exec(e);
  return { host: normHost(m[1]), port: m[2] ? Number(m[2]) : null };
}

// denyHosts entries: "host" (every port) or "host:port" (that port only), the
// second form from servers that send ports; 127.0.0.0/8 is one loopback host.
// port is optional: without it only host-wide entries can match.
export function isDenied(host, denyHosts, port = null) {
  const h = normHost(host);
  return (denyHosts || []).some(d => {
    const e = splitEntry(d);
    if (!e.host) return false;
    const same = e.host === h || (isLoopback(e.host) && isLoopback(h));
    return same && (e.port === null || e.port === port);
  });
}

// "host:port" for a URL (default port from the scheme), or '' -- the paired
// server's own address as a deny entry.
export function endpointOf(url) {
  try {
    const u = new URL(url);
    const h = normHost(u.hostname);
    const port = u.port ? Number(u.port) : (u.protocol === 'https:' ? 443 : 80);
    return h.includes(':') ? `[${h}]:${port}` : `${h}:${port}`;
  } catch { return ''; }
}

// The same check for a full URL (the port defaults from the scheme).
export function isDeniedUrl(url, denyHosts) {
  try {
    const u = new URL(url);
    const port = u.port ? Number(u.port) : (u.protocol === 'https:' ? 443 : 80);
    return isDenied(u.hostname, denyHosts, port);
  } catch { return false; }
}

// An element id encodes the frame it lives in: "f<frameIndex>:<n>", where the
// frame index comes from the last read_page of that tab and <n> is the
// element's number within that frame. A bare integer means the top frame.
// -> { frame, n, id } (canonical string) or throws VerbError.
// One change signature for a tab from its frames' signatures ("hex8:count"
// each, top frame first): a hash of them joined, and the summed count.
export function combineSigs(sigs) {
  const ok = (sigs || []).filter(x => typeof x === 'string' && /^[0-9a-f]{8}:\d{1,7}$/.test(x));
  if (!ok.length) return null;
  if (ok.length === 1) return ok[0];
  let h = 0x811c9dc5;
  const j = ok.join('|');
  for (let i = 0; i < j.length; i++) { h ^= j.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  const n = ok.reduce((a, x) => a + Number(x.split(':')[1]), 0);
  return ('0000000' + h.toString(16)).slice(-8) + ':' + Math.min(n, 9999999);
}

export function parseElementId(v) {
  if (typeof v === 'number' && Number.isInteger(v) && !Number.isNaN(v)) {
    if (v < 1 || v > MAX_ELEMENT_N) throw new VerbError(`element ${v} is out of range`);
    return { frame: 0, n: v, id: `f0:${v}` };
  }
  if (typeof v === 'string') {
    const m = /^f(\d{1,3}):(\d{1,6})$/.exec(v.trim());
    if (m) {
      const frame = Number(m[1]); const n = Number(m[2]);
      if (frame > MAX_FRAME_INDEX) throw new VerbError(`frame index ${frame} is out of range`);
      if (n < 1 || n > MAX_ELEMENT_N) throw new VerbError(`element ${n} is out of range`);
      return { frame, n, id: `f${frame}:${n}` };
    }
  }
  throw new VerbError('element must be an id from browser_read_page, e.g. "f0:12"');
}

// A tiny, deliberately conservative eTLD+1: enough to tell "same site" from
// "cross-site" for the frame-consent rule. Not a full public-suffix list; it
// errs toward asking (over-prompting) rather than silently acting.
const MULTI_TLD = new Set([
  'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'me.uk', 'ltd.uk', 'plc.uk',
  'co.jp', 'or.jp', 'ne.jp', 'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au',
  'co.nz', 'org.nz', 'com.br', 'com.cn', 'com.mx', 'com.sg', 'com.hk',
  'com.tw', 'com.tr', 'co.in', 'co.kr', 'co.za', 'com.ar', 'com.ua',
]);
export function registrableDomain(host) {
  let h = String(host || '').toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
  if (!h) return '';
  if (h.includes(':')) return h;                       // IPv6 literal
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return h;      // IPv4 literal
  const parts = h.split('.');
  if (parts.length <= 2) return h;
  if (MULTI_TLD.has(parts.slice(-2).join('.'))) return parts.slice(-3).join('.');
  return parts.slice(-2).join('.');
}

export function sameSite(a, b) {
  const ra = registrableDomain(a); const rb = registrableDomain(b);
  return !!ra && !!rb && ra === rb;
}

// The consent rule for a cross-origin frame: reading is fine within an
// already-consented top-level site, but clicking / typing into a frame whose
// registrable domain differs from the top-level's needs that domain allowed
// too. Returns true when the frame's own site must be consented separately.
export function frameConsentNeeded(topHost, frameHost) {
  if (!frameHost) return false;          // about:blank / srcdoc: part of the top page
  if (!topHost) return true;
  return !sameSite(topHost, frameHost);
}

// Popup adoption: a tab is adopted only when a Jav3-owned tab opened it. The
// operator's own tabs (no opener, or an opener Jav3 does not own) are never
// touched.
export function shouldAdopt(openerTabId, ownTabIds, newTabId) {
  const own = new Set(ownTabIds || []);
  if (newTabId != null && own.has(newTabId)) return false;   // already ours
  return openerTabId != null && own.has(openerTabId);
}

// -> cleaned params (unknown fields dropped) or throws VerbError
export function validate(verb, params, { denyHosts = [] } = {}) {
  if (!Object.prototype.hasOwnProperty.call(VERBS, verb)) throw new VerbError(`unknown action ${JSON.stringify(verb)}`);
  params = params && typeof params === 'object' && !Array.isArray(params) ? params : {};
  const p = {};
  if (TAB_VERBS.has(verb)) p.tab = int(params, 'tab', 1, 2 ** 31 - 1);
  if (verb === 'open_tab' || verb === 'navigate') p.url = checkUrl(params.url, denyHosts);
  else if (verb === 'read_page') {
    p.max_chars = int(params, 'max_chars', 500, PAGE_TEXT_CAP, 8000);
    p.wait_ms = int(params, 'wait_ms', 0, WAIT_CAP_MS, 0);
    if (params.min_elements !== undefined) p.min_elements = int(params, 'min_elements', 1, 300);
    if (params.selector !== undefined) {
      if (typeof params.selector !== 'string' || !params.selector.trim()) {
        throw new VerbError('selector must be a non-empty CSS selector');
      }
      if (params.selector.length > 200) throw new VerbError('selector is too long');
      p.selector = params.selector.trim();
    }
    if (params.mode !== undefined) {
      if (!READ_MODES.includes(params.mode)) throw new VerbError(`mode must be one of ${READ_MODES.join(', ')}`);
      p.mode = params.mode;
    }
  } else if (verb === 'click') {
    // exactly one of element / (x, y); x, y are CSS px of the top frame's
    // viewport (the server converted them from screenshot pixels)
    const hasXY = params.x !== undefined || params.y !== undefined;
    const hasEl = params.element !== undefined && params.element !== null;
    if (hasXY === hasEl) throw new VerbError('give exactly one of element or x, y');
    if (hasEl) p.element = parseElementId(params.element).id;
    else {
      p.x = coord(params, 'x'); p.y = coord(params, 'y');
      // what the screenshot showed at that point (top frame only: a subframe's
      // point is an <iframe>, which clickAt refuses anyway)
      const ex = params.expect;
      if (ex && typeof ex === 'object' && typeof ex.id === 'string') {
        const id = parseElementId(ex.id);
        if (id.frame === 0) p.expect = { n: id.n, label: typeof ex.label === 'string' ? ex.label.slice(0, 80) : '' };
      }
    }
  } else if (verb === 'type') {
    // no element: type into whatever has focus
    if (params.element !== undefined && params.element !== null) p.element = parseElementId(params.element).id;
  } else if (ELEMENT_VERBS.includes(verb)) p.element = parseElementId(params.element).id;
  if (verb === 'type') {
    if (typeof params.text !== 'string' || !params.text) throw new VerbError('text is required');
    if (params.text.length > TEXT_CAP) throw new VerbError(`text is over ${TEXT_CAP} characters`);
    p.text = params.text;
    const sub = params.submit === undefined ? false : params.submit;
    if (typeof sub !== 'boolean') throw new VerbError('submit must be true or false');
    p.submit = sub;
  } else if (verb === 'select') {
    const hasV = params.value !== undefined && params.value !== null;
    const hasL = params.label !== undefined && params.label !== null;
    if (hasV === hasL) throw new VerbError('give exactly one of value or label');
    const k = hasV ? 'value' : 'label';
    if (typeof params[k] !== 'string') throw new VerbError(`${k} must be a string`);
    if (params[k].length > OPTION_CAP) throw new VerbError(`${k} is too long`);
    if (k === 'label' && !params.label.trim()) throw new VerbError('label must not be empty');
    p[k] = params[k];
  } else if (verb === 'key') {
    p.combo = normalizeCombo(params.combo);
  } else if (verb === 'scroll') {
    p.pages = int(params, 'pages', -10, 10, 1);
    if (!p.pages) throw new VerbError('pages must not be 0');
  }
  return p;
}

// Per-site consent. sites: {"example.com": "allow" | "deny", "*.example.org": "allow"}.
// An exact entry wins over a wildcard; a deny wins over an allow at the same level.
export function siteDecision(host, sites) {
  const h = String(host || '').toLowerCase();
  if (!h || !sites) return null;
  if (sites[h] === 'deny' || sites[h] === 'allow') return sites[h];
  let hit = null;
  for (const [k, v] of Object.entries(sites)) {
    if (!k.startsWith('*.')) continue;
    const base = k.slice(2).toLowerCase();
    if (h === base || h.endsWith('.' + base)) {
      if (v === 'deny') return 'deny';
      if (v === 'allow') hit = 'allow';
    }
  }
  return hit;
}

// "example.com", "https://example.com/x", "*.example.com" -> the site key, or null
export function siteKey(entry) {
  let s = String(entry || '').trim().toLowerCase();
  if (!s) return null;
  const wild = s.startsWith('*.');
  if (wild) s = s.slice(2);
  if (/^https?:\/\//.test(s)) s = hostOf(s) || '';
  s = s.replace(/[/:].*$/, '');
  if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/.test(s)) return null;
  return (wild ? '*.' : '') + s;
}

const DOING = {
  open_tab: 'opening', navigate: 'loading', read_page: 'reading', scroll: 'scrolling',
  scroll_to_element: 'scrolling to an element on', screenshot_tab: 'taking a screenshot of',
  close_tab: 'closing a tab on', list_tabs: 'listing its tabs',
  click: 'clicking on', type: 'typing on', select: 'choosing an option on',
  hover: 'hovering on', key: 'pressing a key on', back: 'going back on', forward: 'going forward on',
};
export function describe(verb, host) {
  const d = DOING[verb] || verb;
  if (verb === 'list_tabs') return `Jav3 is ${d}`;
  return `Jav3 is ${d} ${host || 'a page'}`;
}

// Settings → Add computer: "address=<host:port> code=<code>"
export function parseLoginLine(line) {
  const f = {};
  for (const part of String(line || '').trim().replace(/^['"`]|['"`]$/g, '').split(/\s+/)) {
    const i = part.indexOf('=');
    if (i > 0) {
      const k = part.slice(0, i), v = part.slice(i + 1).replace(/^['"`]|['"`]$/g, '');
      if ((k === 'address' || k === 'code') && v) f[k] = v;
    }
  }
  if (!f.address || !f.code) {
    throw new VerbError("that doesn't look like the login line — expected 'address=<host:port> code=<code>' from Settings → Add computer");
  }
  return { address: f.address, code: f.code };
}

export function baseUrl(address) {
  const a = String(address || '').trim().replace(/\/+$/, '');
  if (!a) throw new VerbError('empty server address');
  if (a.includes('://')) {
    if (!/^https?:\/\//.test(a)) throw new VerbError(`unsupported scheme in ${address}`);
    return a;
  }
  return 'http://' + a;
}

export function wsUrl(base) {
  return base.replace(/^http/, 'ws') + '/api/browser/ws';
}
