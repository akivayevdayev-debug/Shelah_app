/**
 * Behavior tests for static/service-worker.js's stale-while-revalidate fetch
 * handler. The worker is a classic script (no exports) that talks to the
 * `self`/`caches`/`fetch` globals, so those three globals are replaced with
 * recording fakes for the duration of each test, the worker file is
 * `require()`d fresh from its real path, and it is driven through the "fetch"
 * event it registers -- the same entry point the browser uses.
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
const path = require('node:path');

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

// The worker reads these three names as bare globals, and does so when an
// event fires (not just at load), so they must stay installed until the test
// ends; t.after() restores whatever was there before.
const WORKER_GLOBALS = ['self', 'caches', 'fetch'];
const ORIGINAL_GLOBALS = Object.fromEntries(WORKER_GLOBALS.map((name) => [name, globalThis[name]]));

function installGlobals(t, fakes) {
    const saved = WORKER_GLOBALS.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
    t.after(() => {
        for (const [name, descriptor] of saved) {
            if (descriptor) Object.defineProperty(globalThis, name, descriptor);
            else delete globalThis[name];
        }
    });
    for (const name of WORKER_GLOBALS) {
        Object.defineProperty(globalThis, name, {
            value: fakes[name], writable: true, configurable: true, enumerable: true,
        });
    }
}

// Loads the worker and returns dispatch(url) -> { responded, waited } for the
// fetch event it registered.
function loadWorker(t, { fetchImpl }) {
    const listeners = {};
    const caches = createFakeCaches();
    installGlobals(t, {
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
    });
    // A classic script with no exports: drop any cached copy so this load
    // re-runs the file and registers its listeners on this test's fake `self`.
    delete require.cache[require.resolve(WORKER_PATH)];
    require(WORKER_PATH);

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

test('a static asset fetched from the network is returned and cached in the shell cache', async (t) => {
    const { caches, dispatch } = loadWorker(t, {
        fetchImpl: async () => new Response('body{}', { status: 200 }),
    });

    const { request, response } = await dispatch('/static/style.css');

    assert.equal(await response.text(), 'body{}');
    const shell = [...caches.stores.keys()].find((name) => name.startsWith('shelah-shell-'));
    assert.ok(shell, 'static assets belong in the shell cache');
    assert.equal(caches.stores.get(shell).has(request.url), true);
});

test('a non-ok static response is returned but never cached', async (t) => {
    const { caches, dispatch } = loadWorker(t, {
        fetchImpl: async () => new Response('missing', { status: 404 }),
    });

    const { response } = await dispatch('/static/missing.css');

    assert.equal(response.status, 404);
    for (const store of caches.stores.values()) assert.equal(store.size, 0);
});

test('a cached asset is served immediately while the refresh is handed to waitUntil', async (t) => {
    let network = 0;
    const { caches, dispatch } = loadWorker(t, {
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

test('a JSON API reply is cached, but one that reports an error in its body is not', async (t) => {
    const replies = {
        '/api/text/good': jsonResponse({ he: 'טקסט' }),
        '/api/text/bad': jsonResponse({ error: 'not found' }),
    };
    const { caches, dispatch } = loadWorker(t, {
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

test('offline with nothing cached: API reads get a JSON 503, assets a plain 503', async (t) => {
    const { dispatch } = loadWorker(t, {
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

test('private API prefixes bypass the cache entirely', async (t) => {
    const { caches, dispatch } = loadWorker(t, {
        fetchImpl: async () => jsonResponse({ prefs: {} }),
    });

    const { response } = await dispatch('/api/user/preferences');

    assert.deepEqual(await response.json(), { prefs: {} });
    for (const store of caches.stores.values()) assert.equal(store.size, 0);
});

test('the fake worker globals are removed again once a test has finished', () => {
    // Runs after the tests above (top-level tests in a file run in order), each
    // of which installed fakes for self/caches/fetch.
    for (const name of WORKER_GLOBALS) {
        assert.equal(globalThis[name], ORIGINAL_GLOBALS[name], `globalThis.${name} leaked out of a test`);
    }
});
