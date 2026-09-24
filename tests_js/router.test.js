/**
 * static/js/router.js -- the SPA's query-string deep-link router.
 *
 * Pins the existing contract (four mutually-exclusive view keys; `date` as an
 * overlay key that survives exclusive view switches; clearRoute wipes the
 * query; popstate trusts the state object it wrote) and the conversation deep
 * link `/?conversation=<id>&cv=mini|overlay|full`, whose keys behave like
 * `date`: they coexist with a view underneath.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

function makeWindow(search = '', pathname = '/') {
    const listeners = {};
    const win = {
        location: { pathname, search },
        history: {
            calls: [],
            pushState(state, _title, url) { this.calls.push(['push', state, url]); setUrl(url); },
            replaceState(state, _title, url) { this.calls.push(['replace', state, url]); setUrl(url); },
        },
        addEventListener(type, fn) { listeners[type] = fn; },
        dispatch(type, event) { listeners[type]?.(event); },
    };
    function setUrl(url) {
        const q = url.indexOf('?');
        win.location.pathname = q === -1 ? url : url.slice(0, q);
        win.location.search = q === -1 ? '' : url.slice(q);
    }
    return win;
}

async function loadRouter(search, pathname) {
    const window = makeWindow(search, pathname);
    const mod = await loadEsmModule('static/js/router.js', { window });
    return { router: mod.namespace, window };
}

// ── existing behavior ───────────────────────────────────────────────────

test('readRoute returns only known, non-empty keys', async () => {
    const { router } = await loadRouter('?text=Berakhot.2a&date=2026-09-23&utm=x&prayer=');
    assert.deepEqual(router.readRoute(), { text: 'Berakhot.2a', date: '2026-09-23' });
});

test('exclusive push drops other view keys but keeps the date overlay', async () => {
    const { router, window } = await loadRouter('?text=Berakhot.2a&date=2026-09-23');
    const next = router.pushRoute({ prayer: 'shacharit' }, { exclusive: true });
    assert.deepEqual(next, { date: '2026-09-23', prayer: 'shacharit' });
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: next }, '/?prayer=shacharit&date=2026-09-23']);
});

test('non-exclusive push merges; null/empty values delete keys; replace uses replaceState', async () => {
    const { router, window } = await loadRouter('?text=Berakhot.2a&date=2026-09-23');
    const next = router.pushRoute({ date: null, chat: 'h1' }, { replace: true });
    assert.deepEqual(next, { text: 'Berakhot.2a', chat: 'h1' });
    assert.equal(window.history.calls.at(-1)[0], 'replace');
    assert.equal(window.location.search, '?text=Berakhot.2a&chat=h1');
});

test('clearRoute wipes the query (replaceState by default, pushState on request)', async () => {
    const { router, window } = await loadRouter('?text=a&date=2026-09-23&conversation=c1&cv=full', '/app');
    assert.deepEqual(router.clearRoute(), {});
    assert.deepEqual(window.history.calls.at(-1), ['replace', { shelahRoute: {} }, '/app']);
    router.clearRoute({ replace: false });
    assert.equal(window.history.calls.at(-1)[0], 'push');
});

test('history failures are swallowed', async () => {
    const { router, window } = await loadRouter('?text=a');
    window.history.pushState = () => { throw new Error('sandboxed'); };
    window.history.replaceState = () => { throw new Error('sandboxed'); };
    assert.deepEqual(router.pushRoute({ prayer: 'p' }), { text: 'a', prayer: 'p' });
    assert.deepEqual(router.clearRoute(), {});
    assert.deepEqual(router.clearRoute({ keep: ['text'] }), { text: 'a' });
});

test('installRouter: popstate trusts its own state object, falls back to the URL', async () => {
    const { router, window } = await loadRouter('?text=fromUrl&conversation=c1&cv=bogus');
    const seen = [];
    router.installRouter((route) => seen.push(route));
    window.dispatch('popstate', { state: { shelahRoute: { prayer: 'p' } } });
    window.dispatch('popstate', { state: null });
    assert.deepEqual(seen, [{ prayer: 'p' }, { text: 'fromUrl', conversation: 'c1', cv: 'overlay' }]);

    router.installRouter('not a function');
    window.dispatch('popstate', { state: null });
    assert.equal(seen.length, 2, 'a non-function handler is ignored');
});

test('ShelahRouter is exposed on window with the same API', async () => {
    const { router, window } = await loadRouter('');
    assert.equal(window.ShelahRouter.readRoute, router.readRoute);
    assert.deepEqual([...window.ShelahRouter.VIEW_KEYS], ['text', 'prayer', 'community', 'chat']);
    assert.deepEqual([...window.ShelahRouter.CONVERSATION_SIZES], ['mini', 'overlay', 'full']);
});

// ── conversation deep link ──────────────────────────────────────────────

test('readRoute: /?conversation=<id>&cv=full', async () => {
    const { router } = await loadRouter('?conversation=c1&cv=full');
    assert.deepEqual(router.readRoute(), { conversation: 'c1', cv: 'full' });
});

test('readRoute: missing or invalid cv with a conversation reads as "overlay"', async () => {
    for (const search of ['?conversation=c1', '?conversation=c1&cv=huge', '?conversation=c1&cv=']) {
        const { router } = await loadRouter(search);
        assert.deepEqual(router.readRoute(), { conversation: 'c1', cv: 'overlay' }, search);
    }
});

test('readRoute: cv without a conversation is ignored', async () => {
    const { router } = await loadRouter('?cv=full&text=a');
    assert.deepEqual(router.readRoute(), { text: 'a' });
});

test('conversation coexists with a view key underneath', async () => {
    const { router } = await loadRouter('?text=Berakhot.2a&conversation=c1&cv=mini');
    assert.deepEqual(router.readRoute(), { text: 'Berakhot.2a', conversation: 'c1', cv: 'mini' });
});

test('pushRoute opens a conversation; the default size stays implicit in the URL', async () => {
    const { router, window } = await loadRouter('?text=a');
    let next = router.pushRoute({ conversation: 'c1' });
    assert.deepEqual(next, { text: 'a', conversation: 'c1', cv: 'overlay' });
    assert.equal(window.location.search, '?text=a&conversation=c1');

    next = router.pushRoute({ cv: 'full' }, { replace: true });
    assert.deepEqual(next, { text: 'a', conversation: 'c1', cv: 'full' });
    assert.equal(window.location.search, '?text=a&conversation=c1&cv=full');

    next = router.pushRoute({ cv: 'giant' });
    assert.equal(next.cv, 'overlay', 'an invalid size normalizes to the default');
    assert.equal(window.location.search, '?text=a&conversation=c1');
});

test('closing the conversation also drops its size; cv alone is never written', async () => {
    const { router, window } = await loadRouter('?text=a&conversation=c1&cv=full');
    assert.deepEqual(router.pushRoute({ conversation: null }), { text: 'a' });
    assert.equal(window.location.search, '?text=a');
    assert.deepEqual(router.pushRoute({ cv: 'mini' }), { text: 'a' });
    assert.equal(window.location.search, '?text=a');
});

test('exclusive view switch keeps the conversation (and date) overlays', async () => {
    const { router, window } = await loadRouter('?chat=h1&date=2026-09-23&conversation=c1&cv=mini');
    const next = router.pushRoute({ text: 'Berakhot.2a' }, { exclusive: true });
    assert.deepEqual(next, { date: '2026-09-23', conversation: 'c1', cv: 'mini', text: 'Berakhot.2a' });
    assert.equal(window.location.search, '?text=Berakhot.2a&date=2026-09-23&conversation=c1&cv=mini');
});

test('exclusive push can change the conversation in the same patch', async () => {
    const { router } = await loadRouter('?text=a&conversation=c1&cv=mini');
    assert.deepEqual(router.pushRoute({ prayer: 'p', conversation: 'c2', cv: 'full' }, { exclusive: true }), {
        conversation: 'c2', cv: 'full', prayer: 'p',
    });
});

test('clearRoute({ keep }) carries only the named keys over', async () => {
    const { router, window } = await loadRouter('?text=a&date=2026-09-23&conversation=c1&cv=full');
    const next = router.clearRoute({ keep: ['conversation', 'cv'] });
    assert.deepEqual(next, { conversation: 'c1', cv: 'full' });
    assert.deepEqual(window.history.calls.at(-1), ['replace', { shelahRoute: next }, '/?conversation=c1&cv=full']);
});

test('clearRoute({ keep: ["cv"] }) without the conversation keeps nothing', async () => {
    const { router, window } = await loadRouter('?text=a&conversation=c1&cv=full');
    assert.deepEqual(router.clearRoute({ keep: ['cv'] }), {});
    assert.equal(window.location.search, '');
});
