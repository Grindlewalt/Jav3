// node --test clients/jav3-browser/test/
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validate, VerbError, VERBS, siteDecision, siteKey, describe, parseLoginLine, baseUrl, wsUrl, hostOf,
  parseElementId, registrableDomain, sameSite, frameConsentNeeded, shouldAdopt,
} from '../lib/verbs.js';

test('closed verb list, unknown fields dropped', () => {
  assert.deepEqual(validate('open_tab', { url: 'https://example.com/a', js: 'x' }), { url: 'https://example.com/a' });
  assert.deepEqual(validate('type', { tab: 3, element: 9, text: 'hi' }), { tab: 3, element: 'f0:9', text: 'hi', submit: false });
  assert.deepEqual(validate('type', { tab: 3, element: 'f2:5', text: 'hi' }), { tab: 3, element: 'f2:5', text: 'hi', submit: false });
  assert.deepEqual(validate('scroll', { tab: 1 }), { tab: 1, pages: 1 });
  assert.deepEqual(validate('scroll_to_element', { tab: 1, element: 'f1:3' }), { tab: 1, element: 'f1:3' });
  assert.deepEqual(validate('read_page', { tab: 1 }), { tab: 1, max_chars: 8000, wait_ms: 0 });
  assert.deepEqual(validate('read_page', { tab: 1, wait_ms: 3000, min_elements: 5, selector: '.x' }),
    { tab: 1, max_chars: 8000, wait_ms: 3000, min_elements: 5, selector: '.x' });
  assert.deepEqual(validate('list_tabs', { tab: 4 }), {});
  assert.equal(Object.keys(VERBS).length, 15);
});

test('element id parsing and frame consent', () => {
  assert.deepEqual(parseElementId('f3:12'), { frame: 3, n: 12, id: 'f3:12' });
  assert.deepEqual(parseElementId(5), { frame: 0, n: 5, id: 'f0:5' });
  for (const bad of ['2', 'x', 'f0:0', 'f0:', 'f1000:1', '', 0, 1.5, true, null, {}]) {
    assert.throws(() => parseElementId(bad), VerbError, JSON.stringify(bad));
  }
  // element verbs need a real id; bad wait/selector rejected
  for (const [v, p] of [
    ['click', { tab: 1, element: '2' }], ['click', { tab: 1, element: 1.5 }],
    ['scroll_to_element', { tab: 1, element: 0 }],
    ['read_page', { tab: 1, wait_ms: 20000 }], ['read_page', { tab: 1, selector: '' }],
    ['read_page', { tab: 1, min_elements: 0 }]]) {
    assert.throws(() => validate(v, p), VerbError, `${v} ${JSON.stringify(p)}`);
  }
  assert.equal(registrableDomain('a.b.example.com'), 'example.com');
  assert.equal(registrableDomain('sub.example.co.uk'), 'example.co.uk');
  assert.equal(registrableDomain('example.com'), 'example.com');
  assert.equal(registrableDomain('10.0.0.5'), '10.0.0.5');
  assert.equal(sameSite('mail.google.com', 'accounts.google.com'), true);
  assert.equal(sameSite('app.example.com', 'accounts.other.com'), false);
  // clicking a same-site subframe is fine; a different registrable domain is not
  assert.equal(frameConsentNeeded('mail.google.com', 'accounts.google.com'), false);
  assert.equal(frameConsentNeeded('app.example.com', 'login.other.com'), true);
  assert.equal(frameConsentNeeded('example.com', ''), false);   // about:blank subframe
});

test('select / hover / key / back / forward', () => {
  assert.deepEqual(validate('select', { tab: 2, element: 'f0:7', label: 'UK', x: 1 }), { tab: 2, element: 'f0:7', label: 'UK' });
  assert.deepEqual(validate('select', { tab: 2, element: 7, value: '' }), { tab: 2, element: 'f0:7', value: '' });
  assert.deepEqual(validate('hover', { tab: 2, element: 'f1:3' }), { tab: 2, element: 'f1:3' });
  assert.deepEqual(validate('key', { tab: 2, combo: 'Shift+tab' }), { tab: 2, combo: 'shift+Tab' });
  assert.deepEqual(validate('key', { tab: 2, combo: 'cmd+Ctrl+a' }), { tab: 2, combo: 'ctrl+super+a' });
  assert.deepEqual(validate('key', { tab: 2, combo: 'Esc' }), { tab: 2, combo: 'Escape' });
  assert.deepEqual(validate('key', { tab: 2, combo: 'ArrowDown' }), { tab: 2, combo: 'Down' });
  assert.deepEqual(validate('key', { tab: 2, combo: 'f5' }), { tab: 2, combo: 'F5' });
  assert.deepEqual(validate('back', { tab: 2, url: 'x' }), { tab: 2 });
  assert.deepEqual(validate('forward', { tab: 2 }), { tab: 2 });
  assert.equal(VERBS.select, 'act'); assert.equal(VERBS.key, 'act'); assert.equal(VERBS.back, 'read');
  for (const [v, p] of [
    ['select', { tab: 1, element: 'f0:1' }], ['select', { tab: 1, element: 'f0:1', value: 'a', label: 'A' }],
    ['select', { tab: 1, element: 'f0:1', label: ' ' }], ['select', { tab: 1, element: 'f0:1', value: 3 }],
    ['select', { tab: 1, element: 'f0:1', label: 'x'.repeat(501) }], ['select', { tab: 1, label: 'UK' }],
    ['hover', { tab: 1, element: 'nope' }], ['key', { tab: 1, combo: '' }], ['key', { tab: 1 }],
    ['key', { tab: 1, combo: 'hyper+a' }], ['key', { tab: 1, combo: 'Enterr' }],
    ['key', { tab: 1, combo: 'ctrl+alt+shift+super+altgr+a' }], ['key', { tab: 1, combo: 'ctrl+ a' }],
    ['key', { tab: 1, combo: 'F25' }], ['back', {}]]) {
    assert.throws(() => validate(v, p), VerbError, `${v} ${JSON.stringify(p)}`);
  }
  assert.equal(describe('key', 'example.com'), 'Jav3 is pressing a key on example.com');
});

test('popup adoption decision', () => {
  assert.equal(shouldAdopt(7, [7, 8], 9), true);      // opened by a Jav3 tab
  assert.equal(shouldAdopt(5, [7, 8], 9), false);     // operator's tab opened it
  assert.equal(shouldAdopt(null, [7, 8], 9), false);  // no opener: operator's
  assert.equal(shouldAdopt(7, [7, 9], 9), false);     // already ours
});

test('refusals', () => {
  const bad = [
    ['eval', { js: '1' }], ['__proto__', {}], ['toString', {}],
    ['open_tab', { url: 'javascript:alert(1)' }], ['open_tab', { url: 'file:///etc/passwd' }],
    ['open_tab', { url: 'chrome://settings' }], ['open_tab', { url: 'https://u:p@example.com/' }],
    ['open_tab', { url: 'https://example.com/' + 'a'.repeat(2100) }], ['open_tab', {}],
    ['navigate', { url: 'https://example.com/' }],
    ['click', { tab: 1, element: '2' }], ['click', { tab: 1, element: 1.5 }], ['click', { tab: 1, element: 0 }],
    ['click', { tab: 1, element: true }],
    ['type', { tab: 1, element: 2, text: '' }], ['type', { tab: 1, element: 2, text: 'x'.repeat(2001) }],
    ['type', { tab: 1, element: 2, text: 'x', submit: 'yes' }],
    ['scroll', { tab: 1, pages: 0 }], ['scroll', { tab: 1, pages: 11 }],
    ['read_page', { tab: 1, max_chars: 10 }], ['close_tab', null],
  ];
  for (const [verb, params] of bad) {
    assert.throws(() => validate(verb, params), VerbError, `${verb} ${JSON.stringify(params)}`);
  }
});

test('the Jav3 server is never opened', () => {
  const denyHosts = ['jav3.lan', '10.0.0.5', '::1'];
  assert.throws(() => validate('open_tab', { url: 'http://jav3.lan:8000/settings' }, { denyHosts }), /Jav3 server/);
  assert.throws(() => validate('navigate', { tab: 1, url: 'http://[::1]:8000/' }, { denyHosts }), /Jav3 server/);
  assert.ok(validate('open_tab', { url: 'https://example.com/' }, { denyHosts }));
});

test('site consent decisions', () => {
  const sites = { 'example.com': 'allow', '*.corp.test': 'allow', 'bad.corp.test': 'deny', '*.evil.test': 'deny' };
  assert.equal(siteDecision('example.com', sites), 'allow');
  assert.equal(siteDecision('www.example.com', sites), null);   // exact, not subdomains
  assert.equal(siteDecision('a.corp.test', sites), 'allow');
  assert.equal(siteDecision('bad.corp.test', sites), 'deny');
  assert.equal(siteDecision('x.evil.test', sites), 'deny');
  assert.equal(siteDecision('other.test', sites), null);
  assert.equal(siteKey('https://Example.com/path'), 'example.com');
  assert.equal(siteKey('*.example.com'), '*.example.com');
  assert.equal(siteKey('not a site'), null);
});

test('login line and URLs', () => {
  assert.deepEqual(parseLoginLine('address=jav3.lan:8000 code=abc-def'), { address: 'jav3.lan:8000', code: 'abc-def' });
  assert.deepEqual(parseLoginLine("'address=https://j.example code=x'"), { address: 'https://j.example', code: 'x' });
  assert.throws(() => parseLoginLine('code=x'), VerbError);
  assert.equal(wsUrl(baseUrl('jav3.lan:8000')), 'ws://jav3.lan:8000/api/browser/ws');
  assert.equal(wsUrl(baseUrl('https://j.example/')), 'wss://j.example/api/browser/ws');
  assert.throws(() => baseUrl('ftp://x'), VerbError);
  assert.equal(hostOf('https://[::1]:8/'), '::1');
  assert.equal(describe('click', 'example.com'), 'Jav3 is clicking on example.com');
});

test('read_page mode: auto / all / interactive, anything else refused', () => {
  assert.deepEqual(validate('read_page', { tab: 1, mode: 'all' }), { tab: 1, max_chars: 8000, wait_ms: 0, mode: 'all' });
  assert.equal(validate('read_page', { tab: 1 }).mode, undefined);
  assert.throws(() => validate('read_page', { tab: 1, mode: 'every' }), /mode must be one of auto, all, interactive/);
});
