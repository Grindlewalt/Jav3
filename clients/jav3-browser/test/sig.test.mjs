// node --test clients/jav3-browser/test/
// The change signature (form state, frames) and Enter's implicit submission.
import test from 'node:test';
import assert from 'node:assert/strict';
import '../lib/dom.js';
import { combineSigs } from '../lib/verbs.js';

const D = globalThis.__jav3Dom;
const fdoc = els => ({ querySelectorAll: () => els });
const inp = (type, props = {}) => ({ tagName: 'INPUT', type, value: '', ...props });

test('formState: typing, ticking and selecting change it; values are hashed', () => {
  const t = inp('text');
  const doc = fdoc([t]);
  const a = D.formState(doc);
  t.value = 'hunter2';
  const b = D.formState(doc);
  assert.notEqual(a, b);
  assert.match(b, /^[0-9a-f]{8}$/);
  const c = inp('checkbox', { checked: false });
  const cd = fdoc([c]);
  const c0 = D.formState(cd);
  c.checked = true;
  assert.notEqual(D.formState(cd), c0);
  const o1 = { value: 'a', selected: true }, o2 = { value: 'b', selected: false };
  const sel = { tagName: 'SELECT', options: [o1, o2] };
  const s0 = D.formState(fdoc([sel]));
  o1.selected = false; o2.selected = true;
  assert.notEqual(D.formState(fdoc([sel])), s0);
  const ex = { tagName: 'DIV', getAttribute: n => (n === 'aria-expanded' ? ex.v : null), v: 'false' };
  const e0 = D.formState(fdoc([ex]));
  ex.v = 'true';
  assert.notEqual(D.formState(fdoc([ex])), e0);
});

test('formState: a password contributes only empty / non-empty', () => {
  const p = inp('password', { value: 'abc' });
  const a = D.formState(fdoc([p]));
  p.value = 'a much longer different secret';
  assert.equal(D.formState(fdoc([p])), a);
  p.value = '';
  assert.notEqual(D.formState(fdoc([p])), a);
});

test('signature: form state changes the hash, format unchanged', () => {
  assert.equal(D.signature('hello', 3), D.signature('hello', 3, ''));
  assert.notEqual(D.signature('hello', 3, 'aaaaaaaa'), D.signature('hello', 3, 'bbbbbbbb'));
  assert.match(D.signature('hello', 3, 'aaaaaaaa'), /^[0-9a-f]{8}:3$/);
});

test('combineSigs: one frame passes through, any frame changing changes it', () => {
  assert.equal(combineSigs(['aaaaaaaa:3']), 'aaaaaaaa:3');
  assert.equal(combineSigs([]), null);
  const a = combineSigs(['aaaaaaaa:3', 'bbbbbbbb:2']);
  assert.match(a, /^[0-9a-f]{8}:5$/);
  assert.notEqual(a, combineSigs(['aaaaaaaa:3', 'bbbbbbbc:2']));
});

test('implicitSubmit follows the browser rule', () => {
  const t = inp('text'), t2 = inp('email');
  assert.equal(D.implicitSubmit({ elements: [t] }), true);
  assert.equal(D.implicitSubmit({ elements: [t, t2] }), false);
  assert.equal(D.implicitSubmit({ elements: [t, t2, { tagName: 'BUTTON', type: 'submit' }] }), true);
  assert.equal(D.implicitSubmit({ elements: [t, { tagName: 'BUTTON', type: 'submit', disabled: true }] }), false);
  assert.equal(D.implicitSubmit({ elements: [t, t2, { tagName: 'BUTTON', type: 'button' }] }), false);
});
