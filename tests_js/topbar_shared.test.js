/**
 * Tests for static/js/topbar_shared.js -- the two helpers extracted from
 * templates/index.html's and templates/components/legal_scripts.html's
 * toggleLanguage()/positionFloatingMenu() to eliminate their Sonar-flagged
 * (web:S4144) verbatim duplication. Each page's own function still owns
 * everything page-specific (RTL attributes, lang-btn styling, dozens of
 * index.html-only DOM updates, and index.html's CSS-class vs.
 * legal_scripts.html's inline-style way of cancelling the floating menu's
 * centring transform); only the shared prefix/geometry moved here.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { applyLanguagePreference, computeFloatingMenuPosition } = require('../static/js/topbar_shared.js');

function createFakeLocalStorage() {
    const store = new Map();
    return {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
    };
}

function createFakeElement({ id, dataset = {}, rect = {}, isTopbar = false } = {}) {
    return {
        id,
        dataset: { ...dataset },
        textContent: '',
        _isTopbar: isTopbar,
        getBoundingClientRect: () => ({
            left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, ...rect,
        }),
    };
}

// Supports exactly the two selector shapes the module's functions use: a
// comma-separated list of `[data-x]` selectors for querySelectorAll, and
// the single `header[role="banner"]` tag+attribute selector for
// querySelector -- not a general selector engine.
function createFakeDocument(elements) {
    return {
        querySelectorAll(selector) {
            const attrs = selector.split(',').map((part) => {
                const m = /^\[data-([\w-]+)\]$/.exec(part.trim());
                return m ? m[1] : null;
            });
            return elements.filter((el) => attrs.some(
                (attr) => attr && Object.prototype.hasOwnProperty.call(el.dataset, attr),
            ));
        },
        querySelector(selector) {
            if (selector !== 'header[role="banner"]') return null;
            return elements.find((el) => el._isTopbar) || null;
        },
    };
}

function withGlobals(t, { elements = [], innerWidth = 1280, innerHeight = 800 } = {}) {
    const saved = {
        document: globalThis.document,
        localStorage: globalThis.localStorage,
        window: globalThis.window,
    };
    const localStorage = createFakeLocalStorage();
    globalThis.document = createFakeDocument(elements);
    globalThis.localStorage = localStorage;
    globalThis.window = { innerWidth, innerHeight };
    t.after(() => {
        globalThis.document = saved.document;
        globalThis.localStorage = saved.localStorage;
        globalThis.window = saved.window;
    });
    return { localStorage };
}

// ── applyLanguagePreference ─────────────────────────────────────────────

test('applyLanguagePreference persists the language and swaps [data-en]/[data-he] element text', (t) => {
    const el = createFakeElement({ dataset: { en: 'Hello', he: 'שלום' } });
    const { localStorage } = withGlobals(t, { elements: [el] });

    const result = applyLanguagePreference('he', {}, 'preferredLanguageManual');

    assert.equal(result, 'he');
    assert.equal(el.textContent, 'שלום');
    assert.equal(localStorage.getItem('preferredLanguage'), 'he');
    assert.equal(localStorage.getItem('preferredLanguageManual'), '1');
});

test('applyLanguagePreference normalizes any non-"he" value to "en"', (t) => {
    const { localStorage } = withGlobals(t, {});

    assert.equal(applyLanguagePreference('fr', {}, 'preferredLanguageManual'), 'en');
    assert.equal(applyLanguagePreference(undefined, {}, 'preferredLanguageManual'), 'en');
    assert.equal(localStorage.getItem('preferredLanguage'), 'en');
});

test('applyLanguagePreference does not mark the change manual when options.auto is set', (t) => {
    const { localStorage } = withGlobals(t, {});

    applyLanguagePreference('he', { auto: true }, 'preferredLanguageManual');

    assert.equal(localStorage.getItem('preferredLanguageManual'), null);
});

test('applyLanguagePreference marks the change manual when options.auto is absent', (t) => {
    const { localStorage } = withGlobals(t, {});

    applyLanguagePreference('he', {}, 'preferredLanguageManual');

    assert.equal(localStorage.getItem('preferredLanguageManual'), '1');
});

test('applyLanguagePreference leaves an element untouched when it has no text for the target language', (t) => {
    const el = createFakeElement({ dataset: { en: 'Hello' } }); // no `he`
    el.textContent = 'placeholder';
    withGlobals(t, { elements: [el] });

    applyLanguagePreference('he', {}, 'preferredLanguageManual');

    assert.equal(el.textContent, 'placeholder');
});

// ── computeFloatingMenuPosition ─────────────────────────────────────────

test('computeFloatingMenuPosition returns null when the menu or trigger is missing', (t) => {
    withGlobals(t, {});
    assert.equal(computeFloatingMenuPosition(null, createFakeElement()), null);
    assert.equal(computeFloatingMenuPosition(createFakeElement(), null), null);
});

test('computeFloatingMenuPosition centers the panel under the trigger and anchors to the topbar bottom', (t) => {
    const topbar = createFakeElement({ isTopbar: true, rect: { bottom: 64 } });
    const trigger = createFakeElement({ rect: { left: 500, width: 40, bottom: 60 } });
    const menu = createFakeElement();
    withGlobals(t, { elements: [topbar], innerWidth: 1280, innerHeight: 800 });

    const pos = computeFloatingMenuPosition(menu, trigger, { minWidth: 160, maxWidth: 352 });

    assert.equal(pos.width, 352);
    assert.equal(pos.left, Math.round(500 + 20 - 176));
    assert.equal(pos.top, 64, 'anchored to the topbar bottom, not the trigger bottom');
    assert.equal(pos.maxWidth, 1280 - 16);
});

test("computeFloatingMenuPosition falls back to the trigger's own bottom when there is no topbar", (t) => {
    const trigger = createFakeElement({ rect: { left: 100, width: 40, bottom: 90 } });
    const menu = createFakeElement();
    withGlobals(t, { elements: [] });

    const pos = computeFloatingMenuPosition(menu, trigger, {});

    assert.equal(pos.top, 90);
});

test('computeFloatingMenuPosition clamps the panel to the left padding instead of overflowing the viewport edge', (t) => {
    const topbar = createFakeElement({ isTopbar: true, rect: { bottom: 64 } });
    const triggerNearLeftEdge = createFakeElement({ rect: { left: 0, width: 20, bottom: 60 } });
    const menu = createFakeElement();
    withGlobals(t, { elements: [topbar], innerWidth: 400, innerHeight: 800 });

    const pos = computeFloatingMenuPosition(menu, triggerNearLeftEdge, { padding: 8, minWidth: 160, maxWidth: 352 });

    assert.equal(pos.left, 8);
});

test('computeFloatingMenuPosition respects a custom offset from the topbar bottom', (t) => {
    const topbar = createFakeElement({ isTopbar: true, rect: { bottom: 64 } });
    const trigger = createFakeElement({ rect: { left: 100, width: 40, bottom: 60 } });
    const menu = createFakeElement();
    withGlobals(t, { elements: [topbar], innerWidth: 1280, innerHeight: 800 });

    const pos = computeFloatingMenuPosition(menu, trigger, { offset: 10 });

    assert.equal(pos.top, 74);
});

test('the fake document/localStorage/window are removed again once a test has finished', () => {
    assert.equal(typeof globalThis.document === 'undefined' || globalThis.document.querySelector === undefined, true);
});
