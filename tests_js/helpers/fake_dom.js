/**
 * Minimal hand-built DOM fake for tests_js/zmanim.test.js — the first
 * extraction under plan.md §19 whose functions touch `document` directly
 * (getElementById/querySelectorAll + classList/style/dataset/attribute
 * reads and writes), unlike Phase 0/1's DOM-free ai-service.js. Kept
 * intentionally narrow: it implements only what static/js/zmanim.js's
 * Phase 2 exports actually call, not a general DOM shim.
 */
'use strict';

function createClassList(initial = []) {
    const set = new Set(initial);
    return {
        add: (...names) => names.forEach((n) => set.add(n)),
        remove: (...names) => names.forEach((n) => set.delete(n)),
        toggle(name, force) {
            const shouldHave = force === undefined ? !set.has(name) : Boolean(force);
            if (shouldHave) set.add(name);
            else set.delete(name);
            return shouldHave;
        },
        contains: (name) => set.has(name),
    };
}

function createFakeElement(id) {
    const attributes = new Map();
    const styleProps = new Map();
    const children = [];

    const el = {
        id,
        classList: createClassList(),
        style: {
            setProperty: (name, value) => styleProps.set(name, value),
            removeProperty: (name) => styleProps.delete(name),
            getPropertyValue: (name) => styleProps.get(name) || '',
        },
        dataset: {},
        textContent: '',
        innerText: '',
        innerHTML: '',
        title: '',
        disabled: false,
        value: '',
        setAttribute: (name, value) => attributes.set(name, String(value)),
        getAttribute: (name) => (attributes.has(name) ? attributes.get(name) : null),
        removeAttribute: (name) => attributes.delete(name),
        hasAttribute: (name) => attributes.has(name),
        addEventListener: () => {},
        removeEventListener: () => {},
        querySelector: (selector) => children.find((c) => c._tagName === selector.toLowerCase()) || null,
        // Element.append(...nodes): only tracks the nodes for querySelector's
        // benefit, same as addFakeChild -- no real DOM tree is maintained.
        append: (...nodes) => nodes.forEach((node) => node && children.push(node)),
        _children: children,
    };
    return el;
}

// Registers a fake child (e.g. the <span> inside a zman row) so the
// element's own querySelector('span') can find it, mirroring the
// row.querySelector('span')?.textContent usage in startCountdown().
function addFakeChild(parent, tagName, props = {}) {
    const child = createFakeElement(props.id);
    child._tagName = tagName.toLowerCase();
    Object.assign(child, props);
    parent._children.push(child);
    return child;
}

function createFakeDocument() {
    const elementsById = new Map();

    function getElementById(id) {
        if (!elementsById.has(id)) {
            elementsById.set(id, createFakeElement(id));
        }
        return elementsById.get(id);
    }

    // Only supports the one selector shape zmanim.js actually queries:
    // an attribute-presence selector like '[data-zman-row]'.
    function querySelectorAll(selector) {
        const match = /^\[([\w-]+)\]$/.exec(selector);
        if (!match) return [];
        const attr = match[1];
        return Array.from(elementsById.values()).filter((el) => el.hasAttribute(attr));
    }

    // Detached (not id-registered): showZmanimLoadError() builds a <span>
    // and a retry <button> this way before appending them to a real element.
    function createElement(tagName) {
        const el = createFakeElement(undefined);
        el._tagName = String(tagName || '').toLowerCase();
        return el;
    }

    return {
        getElementById,
        querySelectorAll,
        createElement,
        addEventListener: () => {},
        removeEventListener: () => {},
        readyState: 'complete',
    };
}

function createFakeLocalStorage() {
    const store = new Map();
    return {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
        removeItem: (key) => store.delete(key),
        clear: () => store.clear(),
    };
}

// A setTimeout/clearTimeout pair that RECORDS scheduled calls instead of
// firing them. startCountdown() reschedules itself recursively via
// setTimeout — an immediately-firing fake (as used in ai_service.test.js)
// would recurse forever. Tests can fire a queued call manually via
// `timer.setTimeout.calls[i].fn()` when they need to observe one more tick.
function createRecordingTimer() {
    let nextId = 1;
    const calls = [];
    const fakeSetTimeout = (fn, delay) => {
        const id = nextId++;
        calls.push({ id, fn, delay });
        return id;
    };
    const fakeClearTimeout = (id) => {
        const idx = calls.findIndex((c) => c.id === id);
        if (idx >= 0) calls.splice(idx, 1);
    };
    fakeSetTimeout.calls = calls;
    return { setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout };
}

module.exports = {
    createFakeElement,
    addFakeChild,
    createFakeDocument,
    createFakeLocalStorage,
    createRecordingTimer,
};
