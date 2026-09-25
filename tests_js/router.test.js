/**
 * static/js/router.js -- the SPA's deep-link router (paths + query).
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

const urlOf = (win) => win.location.pathname + win.location.search;

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
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: next }, '/prayer/shacharit?date=2026-09-23']);
});

test('non-exclusive push merges; null/empty values delete keys; replace uses replaceState', async () => {
    const { router, window } = await loadRouter('?text=Berakhot.2a&date=2026-09-23');
    const next = router.pushRoute({ date: null, chat: 'h1' }, { replace: true });
    assert.deepEqual(next, { text: 'Berakhot.2a', chat: 'h1' });
    assert.equal(window.history.calls.at(-1)[0], 'replace');
    assert.equal(urlOf(window), '/text/Berakhot.2a?chat=h1', 'the first view key takes the path');
});

test('clearRoute wipes the query (replaceState by default, pushState on request)', async () => {
    const { router, window } = await loadRouter('?text=a&date=2026-09-23&conversation=c1&cv=full', '/app');
    assert.deepEqual(router.clearRoute(), {});
    assert.deepEqual(window.history.calls.at(-1), ['replace', { shelahRoute: {} }, '/app']);
    router.pushRoute({ conversation: 'c2' }, { replace: true });
    router.clearRoute({ replace: false });
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: {} }, '/app']);
});

test('a push to the URL already showing replaces it: no duplicate Back entry', async () => {
    const { router, window } = await loadRouter('', '/history');
    router.pushRoute({ history: '1' }, { exclusive: true });
    assert.deepEqual(window.history.calls, [['replace', { shelahRoute: { history: '1' } }, '/history']]);

    router.pushRoute({ chat: '0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88' }, { exclusive: true });
    router.pushRoute({ chat: '0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88' }, { exclusive: true });
    assert.deepEqual(window.history.calls.slice(1).map(([kind, , url]) => [kind, url]), [
        ['push', '/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88'],
        ['replace', '/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88'],
    ]);

    router.pushPath('/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88');
    assert.equal(window.history.calls.at(-1)[0], 'replace');
    router.pushPath('/history');
    assert.deepEqual(window.history.calls.at(-1).slice(0, 1).concat(window.history.calls.at(-1)[2]), ['push', '/history']);
});

test('hydrating a popstate route through the app\'s own pushes adds no history entry', async () => {
    // index.html's hydrateRoute re-renders the route it is handed; any push
    // that re-render makes lands on the URL Back just restored.
    const { router, window } = await loadRouter('', '/text/Genesis.1');
    router.installRouter((route) => router.pushRoute(route, { exclusive: true }));
    const before = window.history.calls.length;
    window.dispatch('popstate', { state: { shelahRoute: { text: 'Genesis.1' } } });
    window.dispatch('popstate', { state: null });
    const writes = window.history.calls.slice(before);
    assert.equal(writes.length, 2);
    assert.ok(writes.every(([kind]) => kind === 'replace'), JSON.stringify(writes));
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
    assert.deepEqual([...window.ShelahRouter.VIEW_KEYS], ['text', 'prayer', 'community', 'chat', 'a', 'history']);
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
    assert.equal(urlOf(window), '/text/a?conversation=c1');

    next = router.pushRoute({ cv: 'full' }, { replace: true });
    assert.deepEqual(next, { text: 'a', conversation: 'c1', cv: 'full' });
    assert.equal(urlOf(window), '/text/a?conversation=c1&cv=full');

    next = router.pushRoute({ cv: 'giant' });
    assert.equal(next.cv, 'overlay', 'an invalid size normalizes to the default');
    assert.equal(urlOf(window), '/text/a?conversation=c1');
});

test('closing the conversation also drops its size; cv alone is never written', async () => {
    const { router, window } = await loadRouter('?text=a&conversation=c1&cv=full');
    assert.deepEqual(router.pushRoute({ conversation: null }), { text: 'a' });
    assert.equal(urlOf(window), '/text/a');
    assert.deepEqual(router.pushRoute({ cv: 'mini' }), { text: 'a' });
    assert.equal(urlOf(window), '/text/a');
});

test('exclusive view switch keeps the conversation (and date) overlays', async () => {
    const { router, window } = await loadRouter('?chat=h1&date=2026-09-23&conversation=c1&cv=mini');
    const next = router.pushRoute({ text: 'Berakhot.2a' }, { exclusive: true });
    assert.deepEqual(next, { date: '2026-09-23', conversation: 'c1', cv: 'mini', text: 'Berakhot.2a' });
    assert.equal(urlOf(window), '/text/Berakhot.2a?date=2026-09-23&conversation=c1&cv=mini');
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

// ── display keys: lang / layout / vowels / taamim / translit / size ─────

test('readRoute keeps valid display keys alongside a view', async () => {
    const { router } = await loadRouter('?text=Genesis.1&lang=he&layout=bilingual-reverse&vowels=1&taamim=0&translit=1&size=22');
    assert.deepEqual(router.readRoute(), {
        text: 'Genesis.1', lang: 'he', layout: 'bilingual-reverse', vowels: '1', taamim: '0', translit: '1', size: '22',
    });
});

test('readRoute drops display values outside the allowed set', async () => {
    const { router } = await loadRouter('?text=Genesis.1&lang=fr&layout=stacked&vowels=yes&taamim=2&translit=true&size=99');
    assert.deepEqual(router.readRoute(), { text: 'Genesis.1' });
    for (const size of ['13', '31', '1e1', '018', '-20']) {
        const { router: r } = await loadRouter(`?size=${size}`);
        assert.deepEqual(r.readRoute(), {}, size);
    }
    const { router: edge } = await loadRouter('?size=14&layout=hebrew');
    assert.deepEqual(edge.readRoute(), { size: '14', layout: 'hebrew' });
});

test('exclusive view switch keeps display keys', async () => {
    const { router, window } = await loadRouter('?text=Genesis.1&layout=hebrew&taamim=0');
    const next = router.pushRoute({ prayer: 'shacharit' }, { exclusive: true });
    assert.deepEqual(next, { layout: 'hebrew', taamim: '0', prayer: 'shacharit' });
    assert.equal(urlOf(window), '/prayer/shacharit?layout=hebrew&taamim=0');
});

test('an invalid display value pushed in a patch is dropped, not written', async () => {
    const { router, window } = await loadRouter('?text=Genesis.1');
    router.pushRoute({ layout: 'sideways', vowels: '0' }, { replace: true });
    assert.equal(urlOf(window), '/text/Genesis.1?vowels=0');
});

test('clearRoute keeps display keys (going home keeps the link\'s look) plus any keep keys', async () => {
    const { router, window } = await loadRouter('?text=Genesis.1&date=2026-09-23&lang=he&vowels=0&conversation=c1');
    assert.deepEqual(router.clearRoute(), { lang: 'he', vowels: '0' });
    assert.equal(window.location.search, '?lang=he&vowels=0');

    const second = await loadRouter('?text=Genesis.1&lang=he&conversation=c1&cv=full');
    assert.deepEqual(second.router.clearRoute({ keep: ['conversation', 'cv'] }), { conversation: 'c1', cv: 'full', lang: 'he' });
});

test('DISPLAY_KEYS and READER_LAYOUTS are exported and on window.ShelahRouter', async () => {
    const { router, window } = await loadRouter('');
    assert.deepEqual(router.DISPLAY_KEYS, ['lang', 'layout', 'vowels', 'taamim', 'translit', 'size']);
    assert.deepEqual(window.ShelahRouter.READER_LAYOUTS, ['bilingual', 'bilingual-reverse', 'interleaved', 'english', 'hebrew']);
});

// ── public share link (`a`) ─────────────────────────────────────────────

// A share token's shape (16-64 URL-safe chars, `_` and `-` included),
// plainly not a secret.
const SHARE_TOKEN = `${'t'.repeat(20)}_-`;

test('readRoute: /?a=<share token>', async () => {
    const { router } = await loadRouter(`?a=${SHARE_TOKEN}&lang=he`);
    assert.deepEqual(router.readRoute(), { a: SHARE_TOKEN, lang: 'he' });
});

test('readRoute drops an `a` that is not token-shaped', async () => {
    for (const bad of ['short', 'has.dots.in.it.xxxxxxx', `${'x'.repeat(65)}`, '..%2F..%2Fapi%2Fuser%2Fx']) {
        const { router } = await loadRouter(`?a=${bad}&text=Berakhot.2a`);
        assert.deepEqual(router.readRoute(), { text: 'Berakhot.2a' }, bad);
    }
});

test('`a` is an exclusive view key: opening a text replaces it', async () => {
    const { router, window } = await loadRouter(`?a=${SHARE_TOKEN}&date=2026-09-23`);
    const next = router.pushRoute({ text: 'Berakhot.2a' }, { exclusive: true });
    assert.deepEqual(next, { date: '2026-09-23', text: 'Berakhot.2a' });
    assert.equal(urlOf(window), '/text/Berakhot.2a?date=2026-09-23');
});

test('pushRoute drops a malformed `a` and clears it with null', async () => {
    const { router } = await loadRouter('');
    assert.deepEqual(router.pushRoute({ a: 'not a token' }), {});
    assert.deepEqual(router.pushRoute({ a: SHARE_TOKEN }), { a: SHARE_TOKEN });
    assert.deepEqual(router.pushRoute({ a: null }, { replace: true }), {});
});

// ── paths (deep-link Phase 5) ───────────────────────────────────────────

test('readRoute parses each owned path into the same route a query link gives', async () => {
    const cases = [
        ['/text/Genesis.1', { text: 'Genesis 1' }],
        ['/text/Shulchan_Arukh,_Orach_Chayim.345.1', { text: 'Shulchan Arukh, Orach Chayim 345:1' }],
        ['/text/Shulchan%20Arukh,%20Orach%20Chayim%20345:1', { text: 'Shulchan Arukh, Orach Chayim 345:1' }],
        ['/text/Berakhot.2a.5', { text: 'Berakhot 2a:5' }],
        ['/text/Genesis.1.1-2.3', { text: 'Genesis 1:1-2:3' }],
        ['/text/Rashi_on_Genesis.1.1.1', { text: 'Rashi on Genesis 1:1:1' }],
        ['/text/Pirkei_Avot', { text: 'Pirkei Avot' }],
        ['/prayer/Kabbalat_Shabbat', { prayer: 'Kabbalat Shabbat' }],
        ['/prayer/Kabbalat%20Shabbat', { prayer: 'Kabbalat Shabbat' }],
        ['/prayer/shacharit', { prayer: 'shacharit' }],
        ['/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88', { chat: '0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88' }],
        [`/a/${SHARE_TOKEN}`, { a: SHARE_TOKEN }],
        ['/history', { history: '1' }],
        ['/history/', { history: '1' }],
        ['/calendar/2026-09-25', { date: '2026-09-25' }],
        ['/text/Genesis.1/', { text: 'Genesis 1' }],
    ];
    for (const [pathname, route] of cases) {
        const { router } = await loadRouter('', pathname);
        assert.deepEqual(router.readRoute(), route, pathname);
        assert.deepEqual(router.parsePath(pathname), route, pathname);
    }
});

test('paths the router does not own parse to nothing', async () => {
    for (const pathname of ['/', '/settings', '/profile', '/about', '/text', '/text/', '/history/extra', '/calendar/tomorrow',
        '/a/short', '/Text/Genesis.1', '/text/%E0%A4%A', '', '/api/user/history']) {
        const { router } = await loadRouter('', pathname);
        assert.deepEqual(router.readRoute(), {}, pathname);
    }
});

test('the path view wins over view keys in the query; other query keys still apply', async () => {
    const { router } = await loadRouter('?prayer=shacharit&chat=h1&date=2026-09-23&lang=he&conversation=c1', '/text/Genesis.1');
    assert.deepEqual(router.readRoute(), { text: 'Genesis 1', date: '2026-09-23', lang: 'he', conversation: 'c1', cv: 'overlay' });
});

test('/calendar/<date> is the date overlay: a view query key still opens underneath', async () => {
    const { router } = await loadRouter('?community=sephardi', '/calendar/2026-09-25');
    assert.deepEqual(router.readRoute(), { community: 'sephardi', date: '2026-09-25' });
});

test('routeUrl: the main view takes the path, the rest stays in the query', async () => {
    const { router } = await loadRouter('');
    const cases = [
        [{}, '/'],
        [{ lang: 'he' }, '/?lang=he'],
        [{ text: 'Genesis.1', lang: 'he' }, '/text/Genesis.1?lang=he'],
        [{ text: 'Genesis 2' }, '/text/Genesis.2'],
        [{ text: 'Genesis 1:1-2:3' }, '/text/Genesis.1.1-2.3'],
        [{ text: 'Shulchan Arukh, Orach Chayim 345:1' }, '/text/Shulchan_Arukh,_Orach_Chayim.345.1'],
        [{ text: 'Berakhot 2a:5' }, '/text/Berakhot.2a.5'],
        [{ text: 'Pirkei Avot' }, '/text/Pirkei_Avot'],
        [{ text: 'בראשית א' }, `/text/${encodeURIComponent('בראשית_א')}`],
        [{ prayer: 'Kabbalat Shabbat' }, '/prayer/Kabbalat_Shabbat'],
        [{ text: 'a/b?c#d%e' }, '/text/a%2Fb%3Fc%23d%25e'],
        [{ chat: 'h1' }, '/answer/h1'],
        [{ a: SHARE_TOKEN }, `/a/${SHARE_TOKEN}`],
        [{ history: '1' }, '/history'],
        [{ date: '2026-09-25' }, '/calendar/2026-09-25'],
        [{ date: '2026-09-25', prayer: 'mincha' }, '/prayer/mincha?date=2026-09-25'],
        [{ date: 'soon' }, '/?date=soon'],
        [{ community: 'sephardi', date: '2026-09-25' }, '/calendar/2026-09-25?community=sephardi'],
        [{ conversation: 'c1', cv: 'full' }, '/?conversation=c1&cv=full'],
    ];
    for (const [route, url] of cases) assert.equal(router.routeUrl(route), url, JSON.stringify(route));
});

test('every routeUrl reads back as the same route', async () => {
    for (const route of [
        { text: 'Shulchan Arukh, Orach Chayim 345:1', date: '2026-09-25', lang: 'he' },
        { text: 'a/b?c#d%e' },
        { text: 'Genesis 1:1-2:3' },
        { text: 'Berakhot 2a:5' },
        { text: 'Rashi on Genesis 1:1:1' },
        { text: 'בראשית א' },
        { prayer: 'birkat hamazon' },
        { chat: '0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88', conversation: 'c1', cv: 'mini' },
        { a: SHARE_TOKEN, size: '22' },
        { history: '1' },
        { date: '2026-09-25', community: 'sephardi' },
    ]) {
        const { router: builder } = await loadRouter('');
        const url = builder.routeUrl(route);
        const q = url.indexOf('?');
        const { router } = await loadRouter(q === -1 ? '' : url.slice(q), q === -1 ? url : url.slice(0, q));
        assert.deepEqual(router.readRoute(), route, url);
    }
});

test('pushRoute from a path view writes paths; clearRoute goes home to /', async () => {
    const { router, window } = await loadRouter('', '/text/Genesis.1');
    router.pushRoute({ chat: 'h1' }, { exclusive: true });
    assert.equal(urlOf(window), '/answer/h1');
    router.pushRoute({ date: '2026-09-25' }, { replace: true });
    assert.equal(urlOf(window), '/answer/h1?date=2026-09-25');
    router.pushRoute({ chat: null });
    assert.equal(urlOf(window), '/calendar/2026-09-25');
    router.clearRoute({ replace: false });
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: {} }, '/']);
});

test('on a page the router does not own, a push without a view keeps the path', async () => {
    const { router, window } = await loadRouter('', '/settings');
    router.pushRoute({ lang: 'he' }, { replace: true });
    assert.equal(urlOf(window), '/settings?lang=he');
    router.pushRoute({ text: 'Genesis.1' });
    assert.equal(urlOf(window), '/text/Genesis.1?lang=he', 'opening a view leaves the page');
});

test('installRouter rewrites an old query link to its path once, in place', async () => {
    for (const [search, url] of [
        ['?text=Genesis%201&lang=he', '/text/Genesis.1?lang=he'],
        ['?chat=h1', '/answer/h1'],
        [`?a=${SHARE_TOKEN}`, `/a/${SHARE_TOKEN}`],
        ['?date=2026-09-25', '/calendar/2026-09-25'],
        ['?prayer=shacharit&date=2026-09-25&cv=full', '/prayer/shacharit?date=2026-09-25'],
    ]) {
        const { router, window } = await loadRouter(search);
        const before = router.readRoute();
        window.history.state = { fromApp: true };
        router.installRouter(() => {});
        assert.equal(window.history.calls.length, 1, search);
        const [kind, state, written] = window.history.calls[0];
        assert.equal(kind, 'replace', search);
        assert.equal(written, url, search);
        assert.deepEqual(state, { fromApp: true, shelahRoute: before }, 'other history state is kept');
        assert.deepEqual(router.readRoute(), before, 'the route itself is unchanged');
    }
});

test('installRouter leaves canonical URLs and pages it does not own alone', async () => {
    for (const [search, pathname] of [
        ['', '/'],
        ['?lang=he', '/'],
        ['?lang=he', '/text/Genesis.1'],
        ['', '/history'],
        ['?text=Genesis.1', '/settings'],
        ['', '/about'],
    ]) {
        const { router, window } = await loadRouter(search, pathname);
        router.installRouter(() => {});
        assert.equal(window.history.calls.length, 0, pathname + search);
    }
    const trailing = await loadRouter('', '/history/');
    trailing.router.installRouter(() => {});
    assert.equal(urlOf(trailing.window), '/history', 'a non-canonical spelling of an owned path is fixed');
});

test('installRouter swallows a replaceState failure', async () => {
    const { router, window } = await loadRouter('?chat=h1');
    window.history.replaceState = () => { throw new Error('sandboxed'); };
    assert.doesNotThrow(() => router.installRouter(() => {}));
});

test('pushPath pushes the canonical URL and hands the route to the handler', async () => {
    const { router, window } = await loadRouter('');
    const seen = [];
    router.installRouter((route) => seen.push(route));
    assert.deepEqual(router.pushPath('/answer/h1'), { chat: 'h1' });
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: { chat: 'h1' } }, '/answer/h1']);
    assert.deepEqual(router.pushPath('/?text=Genesis.1&lang=he', { replace: true }), { text: 'Genesis.1', lang: 'he' });
    assert.deepEqual(window.history.calls.at(-1), ['replace', { shelahRoute: { text: 'Genesis.1', lang: 'he' } }, '/text/Genesis.1?lang=he']);
    assert.deepEqual(router.pushPath('/history'), { history: '1' });
    assert.deepEqual(seen, [{ chat: 'h1' }, { text: 'Genesis.1', lang: 'he' }, { history: '1' }]);
});

test('pushPath ignores URLs the router does not own', async () => {
    const { router, window } = await loadRouter('');
    window.location.origin = 'https://shelah.example';
    const seen = [];
    router.installRouter((route) => seen.push(route));
    for (const url of ['/about', 'https://evil.example/text/Genesis.1', 'http://[bad', '/api/user/history']) {
        assert.equal(router.pushPath(url), null, url);
    }
    assert.deepEqual(router.pushPath('https://shelah.example/text/Genesis.1'), { text: 'Genesis 1' });
    assert.equal(window.history.calls.length, 1);
    assert.equal(seen.length, 1);
});

test('pushPath works before installRouter (no handler yet)', async () => {
    const { router, window } = await loadRouter('');
    assert.deepEqual(router.pushPath('/prayer/mincha'), { prayer: 'mincha' });
    assert.equal(urlOf(window), '/prayer/mincha');
});
