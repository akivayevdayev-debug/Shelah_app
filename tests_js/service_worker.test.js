/**
 * Behavior tests for static/service-worker.js's stale-while-revalidate fetch
 * handler. The worker is a classic script (no exports) that talks to the
 * `self`/`caches`/`fetch` globals, so it is evaluated in a vm context with
 * recording fakes and driven through the "fetch" event it registers -- the
 * same entry point the browser uses.
 *
 * Covers both cacheability predicates staleWhileRevalidate() accepts: the
 * default synchronous isCacheableResponse() (static/runtime assets) and the
 * asynchronous isCacheableApiResponse() (JSON API reads), which is why the
 * worker awaits the predicate's result either way.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WORKER_PATH = path.join(__dirname, '..', 'static', 'service-worker.js');
const ORIGIN = 'https://shelah.test';

function createFakeCaches() {
    const stores = new Map();
    const store = (name) => {
        if (!stores.has(name)) stores.set(name, new Map());
        return stores.get(name);
    };
    return {
        stores,
        open: async (name) => ({
            match: async (request) => store(name).get(typeof request === 'string' ? request : request.url),
            put: async (request, response) => {
                store(name).set(typeof request === 'string' ? request : request.url, response);
            },
        }),
        match: async () => undefined,
        keys: async () => [...stores.keys()],
        delete: async (name) => stores.delete(name),
    };
}

// Loads the worker and returns dispatch(url) -> { responded, waited } for the
// fetch event it registered.
function loadWorker({ fetchImpl }) {
    const listeners = {};
    const caches = createFakeCaches();
    const sandbox = {
        self: {
            addEventListener: (type, handler) => {
                listeners[type] = handler;
            },
            location: { origin: ORIGIN },
            skipWaiting: () => {},
            clients: { claim: () => {} },
        },
        caches,
        fetch: fetchImpl,
        Response,
        Request,
        URL,
        Promise,
        console,
    };
    vm.runInNewContext(fs.readFileSync(WORKER_PATH, 'utf8'), sandbox, { filename: WORKER_PATH });

    async function dispatch(pathname) {
        const request = new Request(`${ORIGIN}${pathname}`);
        const event = {
            request,
            responded: null,
            waited: [],
            respondWith(promise) {
                this.responded = promise;
            },
            waitUntil(promise) {
                this.waited.push(promise);
            },
        };
        listeners.fetch(event);
        const response = event.responded ? await event.responded : undefined;
        await Promise.all(event.waited);
        return { request, response, waited: event.waited.length };
    }

    return { caches, dispatch };
}

const jsonResponse = (body, init = {}) =>
    new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
        ...init,
    });

test('a static asset fetched from the network is returned and cached in the shell cache', async () => {
    const { caches, dispatch } = loadWorker({
        fetchImpl: async () => new Response('body{}', { status: 200 }),
    });

    const { request, response } = await dispatch('/static/style.css');

    assert.equal(await response.text(), 'body{}');
    const shell = [...caches.stores.keys()].find((name) => name.startsWith('shelah-shell-'));
    assert.ok(shell, 'static assets belong in the shell cache');
    assert.equal(caches.stores.get(shell).has(request.url), true);
});

test('a non-ok static response is returned but never cached', async () => {
    const { caches, dispatch } = loadWorker({
        fetchImpl: async () => new Response('missing', { status: 404 }),
    });

    const { response } = await dispatch('/static/missing.css');

    assert.equal(response.status, 404);
    for (const store of caches.stores.values()) assert.equal(store.size, 0);
});

test('a cached asset is served immediately while the refresh is handed to waitUntil', async () => {
    let network = 0;
    const { caches, dispatch } = loadWorker({
        fetchImpl: async () => {
            network += 1;
            return new Response(`v${network}`, { status: 200 });
        },
    });

    const first = await dispatch('/static/app.js');
    assert.equal(await first.response.text(), 'v1');

    const second = await dispatch('/static/app.js');
    assert.equal(await second.response.text(), 'v1', 'stale copy is served first');
    assert.equal(second.waited, 1, 'the background refresh is kept alive via waitUntil');
    assert.equal(network, 2);
    const shell = [...caches.stores.keys()].find((name) => name.startsWith('shelah-shell-'));
    assert.equal(await caches.stores.get(shell).get(second.request.url).text(), 'v2', 'refresh replaced the entry');
});

test('a JSON API reply is cached, but one that reports an error in its body is not', async () => {
    const replies = {
        '/api/text/good': jsonResponse({ he: 'טקסט' }),
        '/api/text/bad': jsonResponse({ error: 'not found' }),
    };
    const { caches, dispatch } = loadWorker({
        fetchImpl: async (request) => replies[new URL(request.url).pathname].clone(),
    });

    const good = await dispatch('/api/text/good');
    const bad = await dispatch('/api/text/bad');

    // Both are handed to the page; only the healthy one is remembered.
    assert.deepEqual(await good.response.json(), { he: 'טקסט' });
    assert.deepEqual(await bad.response.json(), { error: 'not found' });
    const api = [...caches.stores.keys()].find((name) => name.startsWith('shelah-api-'));
    assert.equal(caches.stores.get(api).has(good.request.url), true);
    assert.equal(caches.stores.get(api).has(bad.request.url), false);
});

test('offline with nothing cached: API reads get a JSON 503, assets a plain 503', async () => {
    const { dispatch } = loadWorker({
        fetchImpl: async () => {
            throw new TypeError('offline');
        },
    });

    const api = await dispatch('/api/text/anything');
    assert.equal(api.response.status, 503);
    assert.deepEqual(await api.response.json(), { error: 'Offline' });

    const asset = await dispatch('/static/never-cached.js');
    assert.equal(asset.response.status, 503);
    assert.equal(await asset.response.text(), '');
});

test('private API prefixes bypass the cache entirely', async () => {
    const { caches, dispatch } = loadWorker({
        fetchImpl: async () => jsonResponse({ prefs: {} }),
    });

    const { response } = await dispatch('/api/user/preferences');

    assert.deepEqual(await response.json(), { prefs: {} });
    for (const store of caches.stores.values()) assert.equal(store.size, 0);
});
