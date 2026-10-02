/**
 * static/js/commentary-preload.js -- finds the verse the reader is on and
 * preloads its commentary: which row holds the reading line, when requests
 * start (after scrolling settles, links before text, the next verses after a
 * pause), and when they must not (reader moved on, rate-limited, data saver,
 * tab hidden, text without commentary).
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/commentary-preload.js', { window: {}, document: {} });
    return mod.namespace;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

// A fake page: verse rows stacked at the given tops, a manual clock, and the
// document/window listener tables the preloader registers on.
function makeEnv({ tops = [0, 100, 200, 300, 400, 500, 600], visible = 'visible', saveData = false } = {}) {
    const rows = tops.map((top, i) => ({
        top,
        ref: `Genesis 1:${i + 1}`,
        getBoundingClientRect() { return { top: this.top }; },
    }));
    const listeners = new Map();
    const scrollingElement = {};
    const doc = {
        visibilityState: visible,
        scrollingElement,
        documentElement: {},
        body: {},
        querySelectorAll: () => rows,
        addEventListener: (type, fn) => listeners.set(`doc:${type}`, fn),
        removeEventListener: (type) => listeners.delete(`doc:${type}`),
    };
    const win = {
        innerHeight: 1000,
        navigator: { connection: { saveData } },
        addEventListener: (type, fn) => listeners.set(`win:${type}`, fn),
        removeEventListener: (type) => listeners.delete(`win:${type}`),
    };
    const clock = { now: 1000, timers: [], nextId: 1 };
    const timers = {
        now: () => clock.now,
        setTimer: (fn, ms) => {
            const id = clock.nextId++;
            clock.timers.push({ id, at: clock.now + ms, fn });
            return id;
        },
        clearTimer: (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); },
        async advance(ms) {
            const target = clock.now + ms;
            for (;;) {
                const due = clock.timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at)[0];
                if (!due) break;
                clock.timers = clock.timers.filter((t) => t !== due);
                clock.now = due.at;
                await due.fn();
                await flush();
            }
            clock.now = target;
            await flush();
        },
        pending: () => clock.timers.length,
    };
    return { rows, doc, win, listeners, timers, clock, scrollingElement };
}

function setup(env, ns, overrides = {}) {
    const calls = [];
    const readingRefs = [];
    const preloader = ns.createCommentaryPreloader({
        doc: env.doc,
        win: env.win,
        getScrollEl: () => env.scrollingElement,
        resolveRef: (row) => row.ref,
        loadLinks: async (ref) => { calls.push(`links:${ref}`); },
        loadTexts: async (ref) => { calls.push(`texts:${ref}`); },
        onReadingRef: (ref) => readingRefs.push(ref),
        now: env.timers.now,
        setTimer: env.timers.setTimer,
        clearTimer: env.timers.clearTimer,
        ...overrides,
    });
    return { preloader, calls, readingRefs };
}

// ── pickReadingIndex / readingLineY ────────────────────────────────────────

test('pickReadingIndex returns the row holding the line, else the last row above it', async () => {
    const ns = await load();
    const tops = [0, 100, 200, 300];
    const rectAt = (i) => ({ top: tops[i] });
    assert.equal(ns.pickReadingIndex(4, rectAt, 150), 1, 'inside row 1');
    assert.equal(ns.pickReadingIndex(4, rectAt, 100), 1, 'exactly at row 1\'s top');
    assert.equal(ns.pickReadingIndex(4, rectAt, 299), 2, 'the gap above row 3 belongs to row 2');
    assert.equal(ns.pickReadingIndex(4, rectAt, 5000), 3, 'past the end: the last row');
    assert.equal(ns.pickReadingIndex(4, rectAt, -50), 0, 'above everything: the first row');
    assert.equal(ns.pickReadingIndex(0, rectAt, 10), -1, 'no rows');
});

test('pickReadingIndex reads only a logarithmic number of rects', async () => {
    const ns = await load();
    let reads = 0;
    const idx = ns.pickReadingIndex(1024, (i) => { reads += 1; return { top: i * 50 - 30000 }; }, 0);
    assert.equal(idx, 600);
    assert.ok(reads <= 11, `read ${reads} rects`);
});

test('readingLineY sits a third of the way down the page or the scrolling element', async () => {
    const ns = await load();
    const doc = { documentElement: {}, body: {}, scrollingElement: {} };
    const win = { innerHeight: 1000 };
    assert.equal(ns.readingLineY(doc.scrollingElement, doc, win), 300);
    assert.equal(ns.readingLineY(null, doc, win), 300);
    const column = { getBoundingClientRect: () => ({ top: 90 }), clientHeight: 800 };
    assert.equal(ns.readingLineY(column, doc, win), 90 + 800 * 0.3);
});

// ── preloader ──────────────────────────────────────────────────────────────

test('loads links then text for the verse in view, then the next verses after a pause', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls, readingRefs } = setup(env, ns);

    preloader.refresh({ immediate: true });
    await flush();
    // The line is at 300: row 3 ("Genesis 1:4") starts exactly there.
    assert.deepEqual(readingRefs, ['Genesis 1:4']);
    assert.deepEqual(calls, ['links:Genesis 1:4', 'texts:Genesis 1:4']);

    await env.timers.advance(ns.NEIGHBOR_MS - 1);
    assert.equal(calls.length, 2, 'the next verses wait for the reader to linger');
    await env.timers.advance(1);
    assert.deepEqual(calls.slice(2), [
        'links:Genesis 1:5', 'texts:Genesis 1:5', 'links:Genesis 1:6',
    ], 'text for the first next verse, links only for the second');
});

test('a scroll is re-evaluated once, after it settles, not on every event', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls, readingRefs } = setup(env, ns);
    preloader.refresh({ immediate: true });
    await flush();

    // Scroll so row 1 is under the line, in several rapid steps.
    const scroll = env.listeners.get('doc:scroll');
    env.rows.forEach((row, i) => { row.top = i * 100 - 100; });
    for (let i = 0; i < 5; i += 1) {
        scroll();
        await env.timers.advance(50);
    }
    assert.deepEqual(readingRefs, ['Genesis 1:4'], 'still debouncing');
    await env.timers.advance(ns.SETTLE_MS);
    assert.deepEqual(readingRefs, ['Genesis 1:4', 'Genesis 1:5']);
    assert.ok(calls.includes('links:Genesis 1:5'));
});

test('staying on the same verse requests nothing more', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls } = setup(env, ns);
    preloader.refresh({ immediate: true });
    await flush();
    const before = calls.length;
    preloader.refresh({ immediate: true });
    env.listeners.get('doc:scroll')();
    await env.timers.advance(ns.SETTLE_MS + 5);
    assert.equal(calls.length, before);
});

test('moving to another verse before the pause cancels the old look-ahead', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls } = setup(env, ns);
    preloader.refresh({ immediate: true });
    await flush();
    assert.deepEqual(calls, ['links:Genesis 1:4', 'texts:Genesis 1:4']);
    assert.equal(env.timers.pending(), 1, 'the look-ahead for 1:5 and 1:6 is waiting');

    // Scroll on to the last verse before the reader has lingered.
    env.rows.forEach((row, i) => { row.top = i * 100 - 600; });
    env.listeners.get('doc:scroll')();
    await env.timers.advance(ns.SETTLE_MS);
    assert.equal(preloader.currentRef(), 'Genesis 1:7');
    await env.timers.advance(ns.NEIGHBOR_MS * 2);
    assert.deepEqual(
        calls.filter((c) => /1:(5|6)$/.test(c)),
        [],
        'the verses after the old position are never requested',
    );
    assert.deepEqual(calls.slice(2), ['links:Genesis 1:7', 'texts:Genesis 1:7']);
});

test('a pressed verse is the verse the reader is on, whatever the scroll position', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls, readingRefs } = setup(env, ns);
    preloader.focusRow(env.rows[0]);
    await flush();
    assert.deepEqual(readingRefs, ['Genesis 1:1']);
    assert.deepEqual(calls.slice(0, 2), ['links:Genesis 1:1', 'texts:Genesis 1:1']);

    // Scrolling hands control back to the reading line.
    env.listeners.get('doc:scroll')();
    await env.timers.advance(ns.SETTLE_MS);
    assert.equal(preloader.currentRef(), 'Genesis 1:4');
});

test('a 429 stops the speculative requests until the back-off passes', async () => {
    const ns = await load();
    const env = makeEnv();
    const calls = [];
    let rateLimited = true;
    const { preloader } = setup(env, ns, {
        loadLinks: async (ref) => {
            calls.push(`links:${ref}`);
            if (rateLimited) throw Object.assign(new Error('HTTP 429'), { status: 429 });
        },
        loadTexts: async (ref) => { calls.push(`texts:${ref}`); },
    });
    preloader.refresh({ immediate: true });
    await flush();
    await env.timers.advance(ns.NEIGHBOR_MS * 2);
    assert.deepEqual(calls, ['links:Genesis 1:4'], 'no text, no look-ahead while backed off');

    rateLimited = false;
    await env.timers.advance(ns.BACKOFF_MS);
    env.rows.forEach((row, i) => { row.top = i * 100 - 100; });
    env.listeners.get('doc:scroll')();
    await env.timers.advance(ns.SETTLE_MS);
    assert.ok(calls.includes('texts:Genesis 1:5'), 'requests resume once the back-off has passed');
});

test('other loader failures are swallowed and do not stop the look-ahead', async () => {
    const ns = await load();
    const env = makeEnv();
    const calls = [];
    const { preloader } = setup(env, ns, {
        loadLinks: async (ref) => { calls.push(`links:${ref}`); throw new Error('boom'); },
        loadTexts: async (ref) => { calls.push(`texts:${ref}`); },
    });
    preloader.refresh({ immediate: true });
    await flush();
    await env.timers.advance(ns.NEIGHBOR_MS);
    assert.ok(calls.includes('links:Genesis 1:5'));
});

test('data saver loads the links for the verse in view and nothing else', async () => {
    const ns = await load();
    const env = makeEnv({ saveData: true });
    const { preloader, calls } = setup(env, ns);
    preloader.refresh({ immediate: true });
    await flush();
    await env.timers.advance(ns.NEIGHBOR_MS * 2);
    assert.deepEqual(calls, ['links:Genesis 1:4']);
});

test('does nothing while the tab is hidden or the text has no commentary', async () => {
    const ns = await load();
    const hidden = makeEnv({ visible: 'hidden' });
    const first = setup(hidden, ns);
    first.preloader.refresh({ immediate: true });
    await flush();
    assert.deepEqual(first.calls, []);

    const env = makeEnv();
    const second = setup(env, ns, { isEligible: () => false });
    second.preloader.refresh({ immediate: true });
    await flush();
    assert.deepEqual(second.calls, []);
    assert.deepEqual(second.readingRefs, []);
});

test('coming back to a hidden tab re-evaluates', async () => {
    const ns = await load();
    const env = makeEnv({ visible: 'hidden' });
    const { preloader, calls } = setup(env, ns);
    preloader.refresh({ immediate: true });
    await flush();
    env.doc.visibilityState = 'visible';
    env.listeners.get('doc:visibilitychange')();
    await env.timers.advance(ns.SETTLE_MS);
    assert.deepEqual(calls.slice(0, 2), ['links:Genesis 1:4', 'texts:Genesis 1:4']);
    assert.equal(preloader.currentRow(), env.rows[3]);
});

test('reset forgets the verse so the same ref is requested afresh; destroy detaches', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls } = setup(env, ns);
    preloader.refresh({ immediate: true });
    await flush();
    const first = calls.length;

    preloader.reset();
    assert.equal(preloader.currentRef(), '');
    preloader.refresh({ immediate: true });
    await flush();
    assert.deepEqual(calls.slice(first, first + 2), ['links:Genesis 1:4', 'texts:Genesis 1:4']);

    assert.ok(env.listeners.has('doc:scroll') && env.listeners.has('win:resize'));
    preloader.destroy();
    assert.equal(env.listeners.size, 0);
    const afterDestroy = calls.length;
    preloader.refresh({ immediate: true });
    preloader.focusRow(env.rows[0]);
    await env.timers.advance(ns.NEIGHBOR_MS * 2);
    assert.equal(calls.length, afterDestroy);
});

test('a row with no ref is skipped; no rows is a no-op', async () => {
    const ns = await load();
    const env = makeEnv();
    const { preloader, calls } = setup(env, ns, { resolveRef: () => '' });
    preloader.refresh({ immediate: true });
    await flush();
    assert.deepEqual(calls, []);

    const empty = makeEnv({ tops: [] });
    const second = setup(empty, ns);
    second.preloader.refresh({ immediate: true });
    await flush();
    assert.deepEqual(second.calls, []);
});
