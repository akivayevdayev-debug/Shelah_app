/**
 * Tests for static/js/actions.js -- the delegated replacement for inline
 * onclick="..." / oninput="..." attributes. The page-level guarantees (every
 * data-onclick in the templates names an allowlisted function, the script is
 * loaded after the click-tracking listener) are pinned by
 * tests/test_inline_handlers.py; this file covers the dispatcher itself.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { ACTIONS, ATTRIBUTES, dispatch, install } = require('../static/js/actions.js');

// A DOM-free stand-in: just enough of Element for closest()/getAttribute().
function el(attrs = {}, { parent = null, disabled = false } = {}) {
    const node = {
        disabled,
        parent,
        hasAttribute: (name) => Object.hasOwn(attrs, name),
        getAttribute: (name) => (Object.hasOwn(attrs, name) ? attrs[name] : null),
        closest(selector) {
            const name = selector.slice(1, -1); // "[data-onclick]" -> "data-onclick"
            for (let n = node; n; n = n.parent) if (n.hasAttribute(name)) return n;
            return null;
        },
    };
    return node;
}

function scopeWith(...names) {
    const calls = [];
    const scope = {};
    for (const name of names) scope[name] = function (...args) { calls.push({ name, args, self: this }); };
    return { scope, calls };
}

test('calls the named function with no argument when there is no -arg attribute', () => {
    const { scope, calls } = scopeWith('goHome');
    const ran = dispatch('click', { target: el({ 'data-onclick': 'goHome' }) }, scope);
    assert.equal(ran, true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].args, []);
    assert.equal(calls[0].self, scope);
});

test('passes the -arg attribute as one string argument', () => {
    const { scope, calls } = scopeWith('readText');
    dispatch('click', { target: el({ 'data-onclick': 'readText', 'data-onclick-arg': 'Genesis 1' }) }, scope);
    assert.deepEqual(calls[0].args, ['Genesis 1']);
});

test('an empty -arg is still an argument, as readText("") was', () => {
    const { scope, calls } = scopeWith('readText');
    dispatch('click', { target: el({ 'data-onclick': 'readText', 'data-onclick-arg': '' }) }, scope);
    assert.deepEqual(calls[0].args, ['']);
});

test('decodes an argument marked data-onclick-encoding="uri"', () => {
    const { scope, calls } = scopeWith('displayCommunity');
    const raw = 'O\'Brien "x" & <b> 50% שמות';
    dispatch('click', {
        target: el({
            'data-onclick': 'displayCommunity',
            'data-onclick-arg': encodeURIComponent(raw),
            'data-onclick-encoding': 'uri',
        }),
    }, scope);
    assert.deepEqual(calls[0].args, [raw]);
});

test('an argument without the uri marker is passed through undecoded', () => {
    const { scope, calls } = scopeWith('readText');
    dispatch('click', { target: el({ 'data-onclick': 'readText', 'data-onclick-arg': 'a%20b' }) }, scope);
    assert.deepEqual(calls[0].args, ['a%20b']);
});

test('finds the handler on an ancestor of the clicked node (an icon inside a button)', () => {
    const { scope, calls } = scopeWith('openLibrarySidebar');
    const button = el({ 'data-onclick': 'openLibrarySidebar' });
    dispatch('click', { target: el({}, { parent: button }) }, scope);
    assert.equal(calls.length, 1);
});

test('the nearest data-onclick wins when handlers are nested', () => {
    const { scope, calls } = scopeWith('goHome', 'readText');
    const outer = el({ 'data-onclick': 'goHome' });
    const inner = el({ 'data-onclick': 'readText', 'data-onclick-arg': 'x' }, { parent: outer });
    dispatch('click', { target: inner }, scope);
    assert.deepEqual(calls.map((c) => c.name), ['readText']);
});

test('ignores a name that is not on the allowlist, even when such a global exists', () => {
    const { scope, calls } = scopeWith('eval', 'fetch', 'alert', 'handlePrivacyDeleteAccountNow');
    for (const name of ['eval', 'fetch', 'alert', 'handlePrivacyDeleteAccountNow', '__proto__', 'constructor', '']) {
        assert.equal(dispatch('click', { target: el({ 'data-onclick': name }) }, scope), false, name);
    }
    assert.equal(calls.length, 0);
});

test('an allowlisted name that is not a function (yet) is a no-op, not a throw', () => {
    assert.equal(dispatch('click', { target: el({ 'data-onclick': 'goHome' }) }, {}), false);
    assert.equal(dispatch('click', { target: el({ 'data-onclick': 'goHome' }) }, { goHome: 'nope' }), false);
});

test('resolves the function when the click happens, as an inline handler did', () => {
    const scope = {};
    const target = el({ 'data-onclick': 'goHome' });
    assert.equal(dispatch('click', { target }, scope), false);
    const calls = [];
    scope.goHome = () => calls.push('late');
    assert.equal(dispatch('click', { target }, scope), true);
    assert.deepEqual(calls, ['late']);
    scope.goHome = () => calls.push('replaced');
    dispatch('click', { target }, scope);
    assert.deepEqual(calls, ['late', 'replaced']);
});

test('a disabled control, or an icon inside one, does not run its action', () => {
    const { scope, calls } = scopeWith('handlePrivacyDeleteAccount');
    const button = el({ 'data-onclick': 'handlePrivacyDeleteAccount' }, { disabled: true });
    assert.equal(dispatch('click', { target: button }, scope), false);
    assert.equal(dispatch('click', { target: el({}, { parent: button }) }, scope), false);
    assert.equal(calls.length, 0);
});

test('click and input read different attributes', () => {
    const { scope, calls } = scopeWith('handlePrivacyDeleteConfirmInput', 'goHome');
    const input = el({ 'data-oninput': 'handlePrivacyDeleteConfirmInput', 'data-onclick': 'goHome' });
    dispatch('input', { target: input }, scope);
    assert.deepEqual(calls.map((c) => c.name), ['handlePrivacyDeleteConfirmInput']);
    dispatch('click', { target: input }, scope);
    assert.deepEqual(calls.map((c) => c.name), ['handlePrivacyDeleteConfirmInput', 'goHome']);
});

test('events without a usable target are ignored', () => {
    const { scope, calls } = scopeWith('goHome');
    assert.equal(dispatch('click', {}, scope), false);
    assert.equal(dispatch('click', { target: null }, scope), false);
    assert.equal(dispatch('click', { target: {} }, scope), false); // no closest(): a text node
    assert.equal(calls.length, 0);
});

test('a throwing action is not swallowed (it reaches window.onerror and Sentry, as an inline handler did)', () => {
    const scope = { goHome() { throw new Error('boom'); } };
    assert.throws(() => dispatch('click', { target: el({ 'data-onclick': 'goHome' }) }, scope), /boom/);
});

test('install registers capture-phase click and input listeners on the document', () => {
    const registered = [];
    const doc = { addEventListener: (type, fn, capture) => registered.push({ type, fn, capture }) };
    const { scope, calls } = scopeWith('goHome');
    install(doc, scope);

    assert.deepEqual(registered.map((r) => [r.type, r.capture]), [['click', true], ['input', true]]);
    registered[0].fn({ target: el({ 'data-onclick': 'goHome' }) });
    assert.equal(calls.length, 1);
});

test('the allowlist is the closed, sorted, frozen set the markup relies on', () => {
    assert.equal(Object.isFrozen(ACTIONS), true);
    assert.equal(Object.isFrozen(ATTRIBUTES), true);
    assert.deepEqual([...ACTIONS], [...ACTIONS].sort());
    assert.equal(new Set(ACTIONS).size, ACTIONS.length);
    assert.deepEqual([...ATTRIBUTES], ['data-onclick', 'data-onclick-arg', 'data-onclick-encoding', 'data-oninput']);
});
