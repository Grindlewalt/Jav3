// The closed verb list, extension side. Pure (no chrome.* calls) so node can
// test it: node --test clients/jav3-browser/test/
// Mirrors backend/browser.py `validate`; both sides check every request.

export const VERBS = Object.freeze({
  open_tab: 'read', navigate: 'read', read_page: 'read', scroll: 'read',
  screenshot_tab: 'read', close_tab: 'read', list_tabs: 'read',
  click: 'act', type: 'act',
});
const TAB_VERBS = new Set(Object.keys(VERBS).filter(v => v !== 'open_tab' && v !== 'list_tabs'));
export const TEXT_CAP = 2000;
export const URL_CAP = 2000;
export const PAGE_TEXT_CAP = 20000;

export class VerbError extends Error {}

function int(params, k, lo, hi, dflt) {
  const v = params[k] === undefined ? dflt : params[k];
  if (typeof v !== 'number' || !Number.isInteger(v)) throw new VerbError(`${k} must be a whole number`);
  if (v < lo || v > hi) throw new VerbError(`${k}=${v} is outside ${lo}..${hi}`);
  return v;
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
  if (isDenied(host, denyHosts)) throw new VerbError('that is the Jav3 server itself; Jav3 never opens it');
  return url;
}

export function isDenied(host, denyHosts) {
  const h = String(host || '').toLowerCase().replace(/^\[|\]$/g, '');
  return (denyHosts || []).some(d => String(d).toLowerCase().replace(/^\[|\]$/g, '') === h);
}

// -> cleaned params (unknown fields dropped) or throws VerbError
export function validate(verb, params, { denyHosts = [] } = {}) {
  if (!Object.prototype.hasOwnProperty.call(VERBS, verb)) throw new VerbError(`unknown action ${JSON.stringify(verb)}`);
  params = params && typeof params === 'object' && !Array.isArray(params) ? params : {};
  const p = {};
  if (TAB_VERBS.has(verb)) p.tab = int(params, 'tab', 1, 2 ** 31 - 1);
  if (verb === 'open_tab' || verb === 'navigate') p.url = checkUrl(params.url, denyHosts);
  else if (verb === 'read_page') p.max_chars = int(params, 'max_chars', 500, PAGE_TEXT_CAP, 8000);
  else if (verb === 'click' || verb === 'type') p.element = int(params, 'element', 1, 100000);
  if (verb === 'type') {
    if (typeof params.text !== 'string' || !params.text) throw new VerbError('text is required');
    if (params.text.length > TEXT_CAP) throw new VerbError(`text is over ${TEXT_CAP} characters`);
    p.text = params.text;
    const sub = params.submit === undefined ? false : params.submit;
    if (typeof sub !== 'boolean') throw new VerbError('submit must be true or false');
    p.submit = sub;
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
  screenshot_tab: 'taking a screenshot of', close_tab: 'closing a tab on',
  list_tabs: 'listing its tabs', click: 'clicking on', type: 'typing on',
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
