// node --test clients/jav3-browser/test/
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validate, VerbError, VERBS, siteDecision, siteKey, describe, parseLoginLine, baseUrl, wsUrl, hostOf,
} from '../lib/verbs.js';

test('closed verb list, unknown fields dropped', () => {
  assert.deepEqual(validate('open_tab', { url: 'https://example.com/a', js: 'x' }), { url: 'https://example.com/a' });
  assert.deepEqual(validate('type', { tab: 3, element: 9, text: 'hi' }), { tab: 3, element: 9, text: 'hi', submit: false });
  assert.deepEqual(validate('scroll', { tab: 1 }), { tab: 1, pages: 1 });
  assert.deepEqual(validate('read_page', { tab: 1 }), { tab: 1, max_chars: 8000 });
  assert.deepEqual(validate('list_tabs', { tab: 4 }), {});
  assert.equal(Object.keys(VERBS).length, 9);
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
