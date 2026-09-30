/**
 * static/js/reader-ui.js installGlobalErrorBoundary(): a page reports each
 * distinct error once and stops after a cap, so an error that repeats on a
 * timer cannot flood /api/client-errors and starve the page's own requests.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function boundary() {
    const listeners = {};
    const posts = [];
    const window = {
        addEventListener: (type, handler) => { listeners[type] = handler; },
        location: { href: 'http://localhost/siddur' },
    };
    const fetch = async (url, options) => {
        posts.push({ url, body: JSON.parse(options.body) });
        return { ok: true };
    };
    const mod = await loadEsmModule('static/js/reader-ui.js', { window, document: {}, fetch });
    mod.namespace.installGlobalErrorBoundary();
    return { listeners, posts };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test('the same error is reported once, however often it fires', async () => {
    const { listeners, posts } = await boundary();
    for (let i = 0; i < 50; i += 1) {
        listeners.error({ message: 'boom', filename: 'a.js', lineno: 3, colno: 9 });
    }
    await flush();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/client-errors');
    assert.equal(posts[0].body.message, 'boom');
});

test('distinct errors each report, up to a cap per page', async () => {
    const { listeners, posts } = await boundary();
    for (let i = 0; i < 40; i += 1) listeners.error({ message: `error ${i}`, filename: 'a.js', lineno: i, colno: 1 });
    listeners.unhandledrejection({ reason: new Error('late') });
    await flush();
    assert.equal(posts.length, 20);
});

test('ResizeObserver noise is never reported', async () => {
    const { listeners, posts } = await boundary();
    listeners.error({ message: 'ResizeObserver loop completed with undelivered notifications.' });
    await flush();
    assert.equal(posts.length, 0);
});
