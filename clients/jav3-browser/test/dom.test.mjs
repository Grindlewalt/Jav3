// node --test clients/jav3-browser/test/
// The pure half of the page functions (lib/dom.js), fed tiny fake elements:
// label resolution, shadow-root traversal, in-view ordering, focus order,
// option picking, key combos and the change signature.
import test from 'node:test';
import assert from 'node:assert/strict';
import '../lib/dom.js';

const D = globalThis.__jav3Dom;

// --- a very small fake DOM ------------------------------------------------------------
const txt = data => ({ nodeType: 3, data, childNodes: [], get textContent() { return data; } });

function descendants(node) {
  const out = [];
  for (const k of node.childNodes) if (k.nodeType === 1) { out.push(k); out.push(...descendants(k)); }
  return out;
}

function matchSimple(e, sel) {
  // 'tag', 'tag[attr]', '[attr="v"]', 'label[for="v"]'
  const m = /^([a-z]*)(?:\[([a-z0-9-]+)(?:="([^"]*)")?\])?$/.exec(sel.trim());
  if (!m) return false;
  if (m[1] && e.tagName.toLowerCase() !== m[1]) return false;
  if (m[2] && e.getAttribute(m[2]) == null) return false;
  if (m[3] !== undefined && e.getAttribute(m[2]) !== m[3]) return false;
  return true;
}

function query(node, sel) {
  const parts = sel.trim().split(/\s+/);
  let set = [node];
  for (const p of parts) set = set.flatMap(n => descendants(n).filter(e => matchSimple(e, p)));
  return set;
}

function root(kids) {
  const r = { nodeType: 11, childNodes: kids };
  kids.forEach(k => { k.parentNode = r; });
  r.querySelectorAll = sel => (sel === '*' ? descendants(r) : query(r, sel));
  r.querySelector = sel => r.querySelectorAll(sel)[0] || null;
  r.getElementById = id => descendants(r).find(e => e.getAttribute('id') === id) || null;
  return r;
}

function el(tag, attrs = {}, kids = [], extra = {}) {
  const e = {
    nodeType: 1, tagName: tag.toUpperCase(), attrs: { ...attrs }, childNodes: kids, ...extra,
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    get id() { return this.attrs.id || ''; },
    get textContent() { return this.childNodes.map(k => k.textContent).join(''); },
    querySelectorAll(sel) { return sel === '*' ? descendants(this) : query(this, sel); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    closest(sel) {
      for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matchSimple(n, sel)) return n;
      return null;
    },
    getRootNode() { let n = this; while (n.parentNode) n = n.parentNode; return n; },
  };
  kids.forEach(k => { k.parentNode = e; });
  return e;
}

test('accessible names: labelledby, label for, wrapping label, icons, placeholder, title', () => {
  const input1 = el('input', { id: 'e', type: 'email' });
  const sel = el('select', {}, [el('option', {}, [txt('US')]), el('option', {}, [txt('UK')])]);
  const iconBtn = el('button', {}, [el('img', { alt: 'Close' })]);
  const svgBtn = el('button', {}, [el('svg', {}, [el('title', {}, [txt('Search')])])]);
  const bare = el('button', {});
  const r = root([
    el('span', { id: 'h1' }, [txt('Billing')]), el('span', { id: 'h2' }, [txt(' address ')]),
    el('input', { 'aria-labelledby': 'h1 h2', id: 'x' }),
    el('label', { for: 'e' }, [txt('Email  address')]), input1,
    el('label', {}, [txt('Country '), sel]),
    el('input', { placeholder: 'Search the docs' }), el('a', { href: '/', title: 'Home' }),
    iconBtn, svgBtn, bare, el('button', { 'aria-label': 'Menu' }, [txt('≡')]),
    el('input', { type: 'submit' }, [], { value: 'Send' }),
  ]);
  const byTag = (i) => r.childNodes[i];
  assert.equal(D.accessibleName(byTag(2)), 'Billing address');
  assert.equal(D.accessibleName(input1), 'Email address');
  assert.equal(D.accessibleName(sel), 'Country');                // option texts not swallowed
  assert.equal(D.accessibleName(byTag(6)), 'Search the docs');
  assert.equal(D.accessibleName(byTag(7)), 'Home');
  assert.equal(D.accessibleName(iconBtn), 'Close');
  assert.equal(D.accessibleName(svgBtn), 'Search');
  assert.equal(D.accessibleName(bare), '');                      // icon-only: kept, unnamed
  assert.equal(D.accessibleName(byTag(11)), 'Menu');
  assert.equal(D.accessibleName(byTag(12)), 'Send');
});

test('deep traversal pierces open shadow roots in document order', () => {
  const inner = el('button', { 'data-jav3-id': '3' }, [txt('Buy')]);
  const host = el('my-card', {}, [el('span', {}, [txt('light')])]);
  host.shadowRoot = root([el('div', {}, [inner])]);
  const r = root([el('a', { href: '/' }), host, el('input', {})]);
  const seen = [];
  D.deepEach(r, e => { seen.push(e.tagName.toLowerCase()); });
  assert.deepEqual(seen, ['a', 'my-card', 'div', 'button', 'span', 'input']);
  assert.equal(D.findJav3(r, 3), inner);
  assert.equal(D.findJav3(r, 4), null);
  let n = 0;
  D.deepEach(r, () => { n += 1; }, { n: 2 });
  assert.equal(n, 2);                                             // node budget honoured
});

test('in view first, then the cap', () => {
  const items = [1, 2, 3, 4, 5, 6].map(i => ({ i, inView: i % 2 === 0 }));
  assert.deepEqual(D.orderInViewFirst(items, 4).map(x => x.i), [2, 4, 6, 1]);
  assert.deepEqual(D.orderInViewFirst(items, 300).map(x => x.i), [2, 4, 6, 1, 3, 5]);
});

test('focus order for Tab: positive tabindex first, skips -1, wraps', () => {
  const items = [{ tabIndex: 0 }, { tabIndex: -1 }, { tabIndex: 2 }, { tabIndex: 0 }, { tabIndex: 1 }];
  const order = D.tabOrder(items);
  assert.deepEqual(order, [4, 2, 0, 3]);
  assert.equal(D.nextInOrder(order, 0, 1), 3);
  assert.equal(D.nextInOrder(order, 3, 1), 4);                    // wraps
  assert.equal(D.nextInOrder(order, 4, -1), 3);                   // shift+Tab wraps back
  assert.equal(D.nextInOrder(order, -1, 1), 4);                   // nothing focused: first
  assert.equal(D.nextInOrder(order, 1, -1), 3);                   // unfocusable current: last
  assert.equal(D.nextInOrder([], 0, 1), -1);
});

test('select options: first 20, chosen starred, picking by value or label', () => {
  const opts = Array.from({ length: 25 }, (_, i) => ({ label: `Opt ${i}`, text: `Opt ${i}`, value: `v${i}`, selected: i === 3 }));
  const s = D.selectOptions({ options: opts }, 20);
  assert.equal(s.options.length, 20);
  assert.equal(s.more, 5);
  assert.deepEqual(s.options[3], { t: 'Opt 3', v: 'v3', s: true });
  assert.equal(D.pickOption(opts, 'v7', null), 7);
  assert.equal(D.pickOption(opts, null, 'Opt 9'), 9);
  assert.equal(D.pickOption(opts, null, '  opt 10 '), 10);        // case / space forgiving
  assert.equal(D.pickOption(opts, 'nope', null), -1);
  assert.equal(D.pickOption(opts, '', null), -1);
});

test('key combos: the desk grammar, browser spellings folded, KeyboardEvent fields', () => {
  assert.equal(D.normalizeCombo('Shift+tab'), 'shift+Tab');
  assert.equal(D.normalizeCombo('meta+Ctrl+Shift+k'), 'ctrl+shift+super+k');
  assert.equal(D.normalizeCombo('esc'), 'Escape');
  assert.equal(D.normalizeCombo('PageDown'), 'Page_Down');
  assert.equal(D.normalizeCombo('Return'), 'Return');
  for (const bad of ['', 'ctrl+', 'hyper+a', 'Enterr', 'F25', 'a+b+c+d+e+f', 'ctrl + a', 5, null]) {
    assert.throws(() => D.normalizeCombo(bad), D.ComboError, JSON.stringify(bad));
  }
  const enter = D.keySpec('Return');
  assert.deepEqual([enter.key, enter.code, enter.keyCode], ['Enter', 'Enter', 13]);
  const st = D.keySpec('shift+Tab');
  assert.deepEqual([st.key, st.shiftKey, st.ctrlKey], ['Tab', true, false]);
  const a = D.keySpec('ctrl+a');
  assert.deepEqual([a.key, a.code, a.keyCode, a.ctrlKey], ['a', 'KeyA', 65, true]);
  assert.equal(D.keySpec('shift+a').key, 'A');
  assert.equal(D.keySpec('cmd+s').metaKey, true);
  assert.deepEqual([D.keySpec('space').key, D.keySpec('space').code], [' ', 'Space']);
  assert.equal(D.keySpec('Down').key, 'ArrowDown');
  assert.equal(D.keySpec('F5').keyCode, 116);
  assert.equal(D.keySpec('7').code, 'Digit7');
});

test('change signature: text hash + element count', () => {
  assert.equal(D.hashText('abc'), D.hashText('abc'));
  assert.notEqual(D.hashText('abc'), D.hashText('abd'));
  assert.match(D.signature('hello', 12), /^[0-9a-f]{8}:12$/);
  assert.notEqual(D.signature('hello', 12), D.signature('hello', 13));
});

test('re-injection keeps one instance', async () => {
  const before = globalThis.__jav3Dom;
  await import('../lib/dom.js?again');
  assert.equal(globalThis.__jav3Dom, before);
});

// --- candidates (no button markup) -------------------------------------------------------

test('candidate reasons: pointer where it starts, click attrs, tabindex, short leaf text', () => {
  const R = D.candidateReason;
  assert.equal(R({ tag: 'div', cursor: 'pointer', parentCursor: 'auto' }), 'pointer');
  // inherited pointer (a <span> inside a pointer <div>) is not another button
  assert.equal(R({ tag: 'span', cursor: 'pointer', parentCursor: 'pointer' }), '');
  assert.equal(R({ tag: 'span', cursor: 'pointer', parentCursor: 'pointer', leafish: true, text: 'Go' }), 'text');
  for (const a of ['onclick', 'ng-click', '(click)', 'jsaction', 'data-action-click', 'data-onclick', '@click']) {
    assert.equal(R({ tag: 'mat-card', attrs: ['class', a] }), 'attr', a);
  }
  assert.equal(R({ tag: 'div', attrs: ['data-id', 'class'] }), '');
  assert.equal(R({ tag: 'div', tabindex: '0' }), 'tabindex');
  assert.equal(R({ tag: 'div', tabindex: '-1' }), '');
  assert.equal(R({ tag: 'div', leafish: true, text: '  Start   assignment ' }), 'text');
  assert.equal(R({ tag: 'div', leafish: true, text: 'x'.repeat(61) }), '');
  assert.equal(R({ tag: 'div', leafish: false, text: 'Start' }), '');
  assert.equal(R({ tag: 'script', cursor: 'pointer', parentCursor: 'auto' }), '');
  assert.equal(R({ tag: 'path', attrs: ['onclick'] }), '');
});

test('candidate visibility: display, visibility, opacity', () => {
  assert.equal(D.styleVisible({ display: 'block', visibility: 'visible', opacity: '1' }), true);
  assert.equal(D.styleVisible({ display: 'none', visibility: 'visible', opacity: '1' }), false);
  assert.equal(D.styleVisible({ display: 'block', visibility: 'hidden', opacity: '1' }), false);
  assert.equal(D.styleVisible({ display: 'block', visibility: 'visible', opacity: '0' }), false);
  assert.equal(D.styleVisible({ display: 'block', visibility: 'visible', opacity: '0.4' }), true);
});

test('candidate dedupe by box containment: a button keeps its label, a card gives way', () => {
  const b = (x, y, w, h) => ({ x, y, w, h });
  // div.btn "Start assignment" holding its <span> label -> the div only
  const btn = [{ box: b(10, 10, 200, 40), text: 'Start assignment' },
               { box: b(20, 20, 120, 20), text: 'Start assignment' }];
  assert.deepEqual(D.dedupeContained(btn), [0]);
  // a long-text card holding two short controls -> the controls
  const card = [{ box: b(0, 0, 500, 300), text: 'Assignment 4 due Friday. '.repeat(5) },
                { box: b(10, 250, 80, 30), text: 'Open' },
                { box: b(100, 250, 80, 30), text: 'Skip' }];
  assert.deepEqual(D.dedupeContained(card), [1, 2]);
  // a text-only toolbar does not swallow its ng-click icon (a real signal)
  const bar = [{ box: b(0, 0, 1280, 40), text: '☰ DeltaMath', why: 'text' },
               { box: b(8, 8, 24, 24), text: '☰', why: 'attr' },
               { box: b(40, 8, 90, 20), text: 'DeltaMath', why: 'text' }];
  assert.deepEqual(D.dedupeContained(bar), [1, 2]);
  // a pointer button still keeps its text-only label out
  assert.deepEqual(D.dedupeContained([{ box: b(0, 0, 100, 30), text: 'Go', why: 'pointer' },
                                      { box: b(5, 5, 20, 20), text: 'Go', why: 'text' }]), [0]);
  // same box twice -> the first; disjoint boxes -> both
  assert.deepEqual(D.dedupeContained([{ box: b(0, 0, 9, 9), text: 'a' }, { box: b(0, 0, 9, 9), text: 'a' }]), [0]);
  assert.deepEqual(D.dedupeContained([{ box: b(0, 0, 9, 9), text: 'a' }, { box: b(20, 0, 9, 9), text: 'b' }]), [0, 1]);
});

test('candidates skip what is already listed, including through shadow hosts', () => {
  const btn = { tagName: 'BUTTON' };
  const sr = { nodeType: 11, parentNode: null, host: btn };
  const inner = { tagName: 'SPAN', parentNode: sr };
  assert.equal(D.insideAny(inner, new Set([btn])), true);
  assert.equal(D.insideAny({ tagName: 'DIV', parentNode: null }, new Set([btn])), false);
  assert.equal(D.leafish({ children: [] }), true);
  assert.equal(D.leafish({ children: [{ children: [] }, { children: [] }] }), true);
  assert.equal(D.leafish({ children: [{ children: [{}] }] }), false);
  assert.equal(D.CAND_CAP, 150);
});

// --- realistic clicks ---------------------------------------------------------------------

test('click sequence: the order and fields a real left click has', () => {
  const seq = D.clickSequence(120.5, 40);
  assert.deepEqual(seq.map(s => s.type), ['pointerover', 'pointerenter', 'mouseover', 'pointermove',
    'pointerdown', 'mousedown', 'focus', 'pointerup', 'mouseup', 'click']);
  for (const s of seq.filter(s => s.init)) {
    assert.equal(s.init.clientX, 120.5); assert.equal(s.init.clientY, 40);
    assert.equal(s.init.button, 0); assert.equal(s.init.composed, true);
    assert.equal(s.init.bubbles, s.type !== 'pointerenter', s.type);
    if (s.ctor === 'PointerEvent') {
      assert.equal(s.init.pointerType, 'mouse'); assert.equal(s.init.isPrimary, true);
    }
  }
  const by = t => seq.find(s => s.type === t);
  assert.equal(by('pointerdown').init.buttons, 1);
  assert.equal(by('mousedown').init.buttons, 1);
  assert.equal(by('pointerup').init.buttons, 0);
  assert.equal(by('click').ctor, 'MouseEvent');
  assert.equal(by('click').init.detail, 1);
  assert.equal(by('focus').ctor, null);
});

// --- F5: click through an overlay ---------------------------------------------------
test('isCovered: only an unrelated element on top counts as an overlay', () => {
  const inner = { contains: () => false };
  const btn = { contains: n => n === inner };
  const wrapper = { contains: n => n === btn || n === wrapper };
  const overlay = { contains: () => false };
  assert.equal(D.isCovered(btn, btn), false);       // itself
  assert.equal(D.isCovered(btn, inner), false);     // a part of it
  assert.equal(D.isCovered(btn, wrapper), false);   // a wrapper of it
  assert.equal(D.isCovered(btn, null), false);
  assert.equal(D.isCovered(btn, overlay), true);
});
