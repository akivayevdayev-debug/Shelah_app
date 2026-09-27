/**
 * Tests for static/js/load-region.js (audit L-8): busy/status marking, the
 * error UI, bounded auto-retry, the online retry, abort on a newer load, and
 * revalidation when the tab comes back.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const flush = () => new Promise((resolve) => setImmediate(resolve));

class FakeNode {
    constructor(doc, tagName) {
        this.ownerDocument = doc;
        this.tagName = tagName;
        this.children = [];
        this.attrs = {};
        this.listeners = {};
        this.parentNode = null;
        this.className = '';
        this._text = '';
        this._html = '';
    }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    insertBefore(child, ref) {
        child.parentNode = this;
        const at = ref ? this.children.indexOf(ref) : -1;
        if (at < 0) this.children.push(child); else this.children.splice(at, 0, child);
        return child;
    }
    get nextSibling() {
        const siblings = this.parentNode?.children || [];
        return siblings[siblings.indexOf(this) + 1] || null;
    }
    get firstChild() { return this.children[0] || null; }
    set textContent(value) { this.children = []; this._html = ''; this._text = String(value); }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
    set innerHTML(value) { this.children = []; this._text = ''; this._html = String(value); }
    get innerHTML() { return this._html; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    removeEventListener(type, handler) { this.listeners[type] = (this.listeners[type] || []).filter((h) => h !== handler); }
    dispatch(type, event = {}) { [...(this.listeners[type] || [])].forEach((h) => h(event)); }
    click() { this.dispatch('click'); }
    find(match) {
        for (const child of this.children) {
            if (match(child)) return child;
            const deeper = child.find(match);
            if (deeper) return deeper;
        }
        return null;
    }
}

function makeDom(lang = 'en') {
    const doc = new FakeNode(null, '#document');
    doc.ownerDocument = doc;
    doc.visibilityState = 'visible';
    doc.documentElement = new FakeNode(doc, 'html');
    doc.documentElement.setAttribute('lang', lang);
    doc.createElement = (tag) => new FakeNode(doc, tag);
    const parent = doc.createElement('main');
    const region = parent.appendChild(doc.createElement('section'));
    return { doc, parent, region };
}

function setup(t, { lang = 'en' } = {}) {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const win = new FakeNode(null, '#window');
    win.navigator = { onLine: true };
    const saved = globalThis.window;
    globalThis.window = win;
    t.after(() => {
        if (saved === undefined) delete globalThis.window; else globalThis.window = saved;
    });
    delete require.cache[require.resolve('../static/js/load-region.js')];
    const api = require('../static/js/load-region.js');
    return { api, win, ...makeDom(lang) };
}

const statusOf = (parent) => parent.find((n) => n.getAttribute('role') === 'status');
const alertOf = (region) => region.find((n) => n.getAttribute('role') === 'alert');
const retryOf = (region) => region.find((n) => n.className === 'region-error__retry');

function failing(times, error = new TypeError('Failed to fetch')) {
    const calls = [];
    const fetch = (signal) => {
        calls.push(signal);
        return calls.length <= times ? Promise.reject(error) : Promise.resolve({ ok: calls.length });
    };
    return { fetch, calls };
}

test('busy while loading, with a separate status line; loaded after', async (t) => {
    const { api, parent, region } = setup(t);
    const rendered = [];
    let resolve;
    const handle = api.loadRegion(region, {
        skeleton: () => '<div class="sk-line"></div>',
        fetch: () => new Promise((r) => { resolve = r; }),
        render: (data, meta) => rendered.push([data, meta]),
    });
    assert.equal(region.getAttribute('aria-busy'), 'true');
    assert.equal(region.innerHTML, '<div class="sk-line"></div>', 'skeleton drawn at once');
    assert.equal(statusOf(parent).textContent, 'Loading…');
    assert.notEqual(region.getAttribute('role'), 'status', 'the status line is not the region');

    resolve({ items: 1 });
    await handle.ready;
    assert.deepEqual(rendered, [[{ items: 1 }, { revalidated: false }]]);
    assert.equal(region.getAttribute('aria-busy'), 'false');
    assert.equal(statusOf(parent).textContent, 'Loaded');
});

test('a failure shows an alert with Retry, retries after 2s and 6s, then waits for the visitor', async (t) => {
    const { api, region } = setup(t);
    const { fetch, calls } = failing(10);
    await api.loadRegion(region, { fetch, render: () => {} }).ready;

    assert.ok(alertOf(region), 'role="alert"');
    assert.match(alertOf(region).textContent, /Failed to fetch/);
    assert.ok(retryOf(region), 'a focusable Retry button');
    assert.equal(region.getAttribute('aria-busy'), 'false');

    t.mock.timers.tick(1999);
    await flush();
    assert.equal(calls.length, 1, 'nothing before 2s');
    t.mock.timers.tick(1);
    await flush();
    assert.equal(calls.length, 2, 'first automatic retry at 2s');
    t.mock.timers.tick(6000);
    await flush();
    assert.equal(calls.length, 3, 'second at 6s');
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(calls.length, 3, 'then manual only');

    retryOf(region).click();
    await flush();
    assert.equal(calls.length, 4, 'Retry fetches again');
});

test('retrying that cannot help: no Retry, no timers, the caller\'s actions instead', async (t) => {
    const { api, win, region } = setup(t);
    const missing = Object.assign(new Error('Prayer not found'), { retryable: false });
    const { fetch, calls } = failing(10, missing);
    let backs = 0;
    await api.loadRegion(region, {
        fetch,
        render: () => {},
        errorActions: () => [{ label: 'Back to prayers', onClick: () => { backs += 1; } }],
    }).ready;

    assert.equal(retryOf(region), null);
    const back = region.find((n) => n.className === 'region-error__action');
    assert.equal(back.textContent, 'Back to prayers');
    back.click();
    assert.equal(backs, 1);
    t.mock.timers.tick(60000);
    win.dispatch('online');
    await flush();
    assert.equal(calls.length, 1);
});

test('back online: one retry, and the pending timer is dropped', async (t) => {
    const { api, win, region } = setup(t);
    const { fetch, calls } = failing(1);
    const rendered = [];
    await api.loadRegion(region, { fetch, render: (data) => rendered.push(data) }).ready;

    win.dispatch('online');
    await flush();
    assert.equal(calls.length, 2);
    assert.deepEqual(rendered, [{ ok: 2 }]);
    t.mock.timers.tick(60000);
    win.dispatch('online');
    await flush();
    assert.equal(calls.length, 2, 'no stray retries after it loaded');
    assert.deepEqual(win.listeners.online, [], 'online listener removed');
});

test('offline when the timer fires: it waits for the connection instead', async (t) => {
    const { api, win, region } = setup(t);
    const { fetch, calls } = failing(1);
    await api.loadRegion(region, { fetch, render: () => {} }).ready;
    win.navigator.onLine = false;
    t.mock.timers.tick(2000);
    await flush();
    assert.equal(calls.length, 1);
    win.navigator.onLine = true;
    win.dispatch('online');
    await flush();
    assert.equal(calls.length, 2);
});

test('a newer load of the region aborts the older one and its retries', async (t) => {
    const { api, win, region } = setup(t);
    const rendered = [];
    let resolveOld;
    const oldCalls = [];
    api.loadRegion(region, {
        fetch: (signal) => { oldCalls.push(signal); return new Promise((r) => { resolveOld = r; }); },
        render: (data) => rendered.push(data),
    });
    const fresh = api.loadRegion(region, { fetch: () => Promise.resolve('new'), render: (data) => rendered.push(data) });
    assert.equal(oldCalls[0].aborted, true);
    await fresh.ready;
    resolveOld('old');
    await flush();
    assert.deepEqual(rendered, ['new'], 'the older answer never lands');

    const { fetch, calls } = failing(10);
    await api.loadRegion(region, { fetch, render: () => {} }).ready;
    api.abortRegion(region);
    t.mock.timers.tick(60000);
    win.dispatch('online');
    await flush();
    assert.equal(calls.length, 1, 'abort clears timers and listeners');
    assert.equal(calls[0].aborted, true);
});

test('revalidate on visible: quiet refetch, and a failed one keeps what is shown', async (t) => {
    const { api, doc, region } = setup(t);
    let n = 0;
    let fail = false;
    const rendered = [];
    await api.loadRegion(region, {
        skeleton: () => 'SKELETON',
        fetch: () => (fail ? Promise.reject(new Error('down')) : Promise.resolve(++n)),
        render: (data, meta) => { rendered.push([data, meta.revalidated]); region.textContent = `v${data}`; },
        revalidateOnVisible: true,
    }).ready;

    doc.visibilityState = 'hidden';
    doc.dispatch('visibilitychange');
    await flush();
    assert.equal(rendered.length, 1, 'hidden: nothing');
    doc.visibilityState = 'visible';
    doc.dispatch('visibilitychange');
    await flush();
    assert.deepEqual(rendered, [[1, false], [2, true]]);
    assert.equal(region.getAttribute('aria-busy'), 'false', 'no busy state or skeleton for a quiet refresh');

    fail = true;
    doc.dispatch('visibilitychange');
    await flush();
    assert.equal(region.textContent, 'v2', 'content kept');
    assert.equal(alertOf(region), null);
    fail = false;
    doc.dispatch('visibilitychange');
    await flush();
    assert.deepEqual(rendered.at(-1), [3, true], 'still watching after a failed refresh');
});

test('retryLater on its own (the reader keeps its own render path)', async (t) => {
    const { api, win, region } = setup(t);
    const attempts = [];
    api.retryLater(region, (next) => attempts.push(next), { attempt: 0 });
    t.mock.timers.tick(2000);
    assert.deepEqual(attempts, [1]);
    api.retryLater(region, (next) => attempts.push(next), { attempt: 2 });
    t.mock.timers.tick(60000);
    assert.deepEqual(attempts, [1], 'past the last delay: no timer');
    win.dispatch('online');
    assert.deepEqual(attempts, [1, 2], 'but the online retry still comes');
    api.retryLater(region, (next) => attempts.push(next), { attempt: 0 });
    api.beginRegionLoad(region);
    t.mock.timers.tick(60000);
    win.dispatch('online');
    assert.deepEqual(attempts, [1, 2], 'a new load cancels pending retries');
});

test('Hebrew labels', async (t) => {
    const { api, parent, region } = setup(t, { lang: 'he' });
    const { fetch } = failing(1);
    api.setRegionBusy(region, true);
    assert.equal(statusOf(parent).textContent, 'טוען…');
    await api.loadRegion(region, { fetch, render: () => {}, errorMessage: () => 'שגיאה' }).ready;
    assert.equal(retryOf(region).textContent, 'נסה שוב');
    assert.equal(alertOf(region).find((n) => n.className === 'region-error__message').textContent, 'שגיאה');
});

test('a load called off ends busy without announcing "Loaded"', (t) => {
    const { api, parent, region } = setup(t);
    api.setRegionBusy(region, true);
    api.setRegionBusy(region, false, { quiet: true });
    assert.equal(region.getAttribute('aria-busy'), 'false');
    assert.equal(statusOf(parent).textContent, '');
    const other = parent.appendChild(parent.ownerDocument.createElement('section'));
    api.setRegionBusy(other, false, { quiet: true });
    assert.equal(parent.children.filter((n) => n.getAttribute('role') === 'status').length, 1,
        'no status line is made just to be emptied');
});

test('one status line per region, however often it loads', (t) => {
    const { api, parent, region } = setup(t);
    api.setRegionBusy(region, true);
    api.setRegionBusy(region, false);
    api.setRegionBusy(region, true);
    assert.equal(parent.children.filter((n) => n.getAttribute('role') === 'status').length, 1);
});
