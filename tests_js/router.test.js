/**
 * static/js/router.js -- the SPA's deep-link router (paths + query).
 *
 * Pins the existing contract (four mutually-exclusive view keys; `date` as an
 * overlay key that survives exclusive view switches; clearRoute wipes the
 * query; popstate trusts the state object it wrote) and the conversation deep
 * link `/chat/<id>[/mini|/full]`, whose keys behave like `date`: they coexist
 * with a view underneath. Everything the app writes is a path segment
 * (router.js "Paths"); only display keys and the history search are queries.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

function withoutSeq(state) {
    if (!state || typeof state !== 'object') return state;
    const { seq: _seq, ...rest } = state;
    return rest;
}

function makeWindow(search = '', pathname = '/') {
    const listeners = {};
    const win = {
        location: { pathname, search },
        history: {
            calls: [],
            state: null,
            // Entries for back(): the fake keeps a real stack so the overlay
            // contract's history.back() can be followed by a popstate.
            entries: [{ state: null, url: pathname + search }],
            index: 0,
            // `seq` is asserted by its own tests; the rest compare the
            // remaining state shape.
            pushState(state, _title, url) {
                this.entries.splice(this.index + 1, Infinity, { state, url });
                this.index += 1;
                this.state = state;
                this.calls.push(['push', withoutSeq(state), url]);
                setUrl(url);
            },
            replaceState(state, _title, url) {
                this.entries[this.index] = { state, url };
                this.state = state;
                this.calls.push(['replace', withoutSeq(state), url]);
                setUrl(url);
            },
            back() {
                this.calls.push(['back']);
                if (this.index === 0) return;
                this.index -= 1;
                const entry = this.entries[this.index];
                this.state = entry.state;
                setUrl(entry.url);
                win.dispatch('popstate', { state: entry.state });
            },
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
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: next }, '/prayer/shacharit/calendar/2026-09-23']);
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
    router.pushRoute({ lang: 'he' }, { replace: true });
    router.clearRoute({ replace: false, patch: { lang: null } });
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
    assert.equal(urlOf(window), '/text/a/chat/c1');

    next = router.pushRoute({ cv: 'full' }, { replace: true });
    assert.deepEqual(next, { text: 'a', conversation: 'c1', cv: 'full' });
    assert.equal(urlOf(window), '/text/a/chat/c1/full');

    next = router.pushRoute({ cv: 'giant' });
    assert.equal(next.cv, 'overlay', 'an invalid size normalizes to the default');
    assert.equal(urlOf(window), '/text/a/chat/c1');
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
    assert.equal(urlOf(window), '/text/Berakhot.2a/chat/c1/mini/calendar/2026-09-23');
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
    assert.deepEqual(window.history.calls.at(-1), ['replace', { shelahRoute: next }, '/chat/c1/full']);
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

// ── AI keys: minhag / mode ride along with a conversation or answer ─────

test('readRoute keeps minhag and mode alongside a conversation or an answer', async () => {
    const { router } = await loadRouter('?conversation=c1&minhag=Sefardic&mode=strict');
    assert.deepEqual(router.readRoute(), { conversation: 'c1', cv: 'overlay', minhag: 'Sefardic', mode: 'strict' });
    const answer = await loadRouter('?minhag=Greek-Romaniote&mode=practical', '/answer/h1');
    assert.deepEqual(answer.router.readRoute(), { chat: 'h1', minhag: 'Greek-Romaniote', mode: 'practical' });
});

test('minhag and mode mean nothing without a conversation or answer and are dropped', async () => {
    const { router } = await loadRouter('?text=a&minhag=Sefardic&mode=strict');
    assert.deepEqual(router.readRoute(), { text: 'a' });
});

test('an unknown mode or a malformed minhag is dropped, not guessed at', async () => {
    const { router } = await loadRouter('?chat=h1&minhag=%3Cscript%3E&mode=turbo');
    assert.deepEqual(router.readRoute(), { chat: 'h1' });
});

test('pushRoute writes minhag and mode into the query next to the answer path', async () => {
    const { router, window } = await loadRouter('');
    router.pushRoute({ chat: 'h1', minhag: 'Ashkenaz', mode: 'sources' }, { exclusive: true, overlay: 'answer' });
    assert.equal(urlOf(window), '/answer/h1/ashkenaz/sources');
});

test('exclusive view switch under an open conversation keeps minhag and mode', async () => {
    const { router, window } = await loadRouter('?conversation=c1&minhag=Yemenite&mode=balanced');
    router.pushRoute({ text: 'Berakhot.2a' }, { exclusive: true });
    assert.equal(urlOf(window), '/text/Berakhot.2a/chat/c1/yemenite/balanced');
});

test('closing the conversation also drops minhag and mode', async () => {
    const { router, window } = await loadRouter('?text=a&conversation=c1&minhag=Iraqi&mode=strict');
    assert.deepEqual(router.pushRoute({ conversation: null }), { text: 'a' });
    assert.equal(urlOf(window), '/text/a');
});

test('AI_KEYS and AI_MODES are exported and on window.ShelahRouter', async () => {
    const { router, window } = await loadRouter('');
    assert.deepEqual(router.AI_KEYS, ['minhag', 'mode']);
    assert.deepEqual(window.ShelahRouter.AI_MODES, ['balanced', 'practical', 'sources', 'strict']);
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
    assert.equal(urlOf(window), '/text/Berakhot.2a/calendar/2026-09-23');
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
        ['/community/Greek-Romaniote', { community: 'Greek-Romaniote' }],
        ['/community/Spanish_and_Portuguese', { community: 'Spanish and Portuguese' }],
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
    for (const pathname of ['/', '/about', '/full', '/strict', '/sefardic', '/chat', '/chat/c1/extra/words', '/text/Genesis.1/calendar/soon',
        '/answer/h1/sefardic/ashkenaz', '/text/Genesis.1/full', '/text', '/text/', '/history/extra', '/calendar/tomorrow',
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
        [{ date: '2026-09-25', prayer: 'mincha' }, '/prayer/mincha/calendar/2026-09-25'],
        [{ date: 'soon' }, '/?date=soon'],
        [{ community: 'sephardi', date: '2026-09-25' }, '/community/sephardi/calendar/2026-09-25'],
        [{ community: 'Spanish and Portuguese' }, '/community/Spanish_and_Portuguese'],
        [{ conversation: 'c1', cv: 'full' }, '/chat/c1/full'],
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
        { community: 'Spanish and Portuguese', lang: 'he' },
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
    assert.equal(urlOf(window), '/answer/h1/calendar/2026-09-25');
    router.pushRoute({ chat: null });
    assert.equal(urlOf(window), '/calendar/2026-09-25');
    router.clearRoute({ replace: false });
    assert.deepEqual(window.history.calls.at(-1), ['push', { shelahRoute: {} }, '/']);
});

test('on a page the router does not own, a push without a view keeps the path', async () => {
    const { router, window } = await loadRouter('', '/app');
    router.pushRoute({ lang: 'he' }, { replace: true });
    assert.equal(urlOf(window), '/app?lang=he');
    router.pushRoute({ text: 'Genesis.1' });
    assert.equal(urlOf(window), '/text/Genesis.1?lang=he', 'opening a view leaves the page');
});

test('installRouter rewrites an old query link to its path once, in place', async () => {
    for (const [search, url] of [
        ['?text=Genesis%201&lang=he', '/text/Genesis.1?lang=he'],
        ['?chat=h1', '/answer/h1'],
        [`?a=${SHARE_TOKEN}`, `/a/${SHARE_TOKEN}`],
        ['?date=2026-09-25', '/calendar/2026-09-25'],
        ['?prayer=shacharit&date=2026-09-25&cv=full', '/prayer/shacharit/calendar/2026-09-25'],
        ['?conversation=c1&cv=full&minhag=sefardic&mode=strict', '/chat/c1/sefardic/strict/full'],
        ['?text=Genesis%201&conversation=new', '/text/Genesis.1/chat/new'],
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
        ['?text=Genesis.1', '/app'],
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

// ── canonical <link> follows in-app navigation (audit U-1) ─────────────

function makeHead(canonicalHref) {
    const nodes = [];
    const el = (attrs) => ({
        attrs: { ...attrs },
        getAttribute(name) { return this.attrs[name] ?? null; },
        setAttribute(name, value) { this.attrs[name] = String(value); },
        remove() { const i = nodes.indexOf(this); if (i !== -1) nodes.splice(i, 1); },
    });
    nodes.push(el({ property: 'og:url', content: 'https://shelah-app.vercel.app/text/Genesis.1' }));
    if (canonicalHref) nodes.push(el({ rel: 'canonical', href: canonicalHref }));
    const doc = {
        head: { appendChild(node) { nodes.push(node); } },
        createElement: () => el({}),
        querySelector(selector) {
            if (selector === 'meta[property="og:url"]') return nodes.find((n) => n.attrs.property === 'og:url') || null;
            if (selector === 'link[rel="canonical"]') return nodes.find((n) => n.attrs.rel === 'canonical') || null;
            if (selector === 'meta[name="robots"][data-not-found]') return robots();
            return null;
        },
    };
    const robots = () => nodes.find((n) => n.attrs.name === 'robots' && 'data-not-found' in n.attrs) || null;
    return {
        doc,
        canonical: () => nodes.find((n) => n.attrs.rel === 'canonical')?.attrs.href ?? null,
        robots: () => robots()?.attrs.content ?? null,
        count: () => nodes.length,
    };
}

async function loadRouterWithHead(search, pathname, canonicalHref) {
    const window = makeWindow(search, pathname);
    const head = makeHead(canonicalHref);
    window.document = head.doc;
    const mod = await loadEsmModule('static/js/router.js', { window });
    return { router: mod.namespace, window, head };
}

test('a view push points the canonical at the new path, without display keys or overlays', async () => {
    const { router, head } = await loadRouterWithHead('', '/text/Genesis.1', 'https://shelah-app.vercel.app/text/Genesis.1');
    router.pushRoute({ prayer: 'Upon Arising', date: '2026-09-25', lang: 'he' }, { exclusive: true });
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/prayer/Upon_Arising');
    router.clearRoute();
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/');
});

test('a private view drops the canonical and a library view brings it back', async () => {
    const { router, head } = await loadRouterWithHead('', '/text/Genesis.1', 'https://shelah-app.vercel.app/text/Genesis.1');
    router.pushRoute({ chat: 'h1' }, { exclusive: true });
    assert.equal(head.canonical(), null);
    router.pushRoute({ text: 'Exodus 2' }, { exclusive: true });
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/text/Exodus.2');
});

test('popstate syncs the canonical to the route it restores', async () => {
    const { router, window, head } = await loadRouterWithHead('', '/history', null);
    router.installRouter(() => {});
    window.location.pathname = '/text/Genesis.1';
    window.dispatch('popstate', { state: { shelahRoute: { text: 'Genesis 1' } } });
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/text/Genesis.1');
});

test('a text the reader found missing is noindex with no canonical, until the next navigation (U-14)', async () => {
    const { router, window, head } = await loadRouterWithHead('', '/text/Blorp.4', 'https://shelah-app.vercel.app/text/Blorp.4');
    router.installRouter(() => {});
    window.ShelahRouter.markNotFound();
    window.ShelahRouter.markNotFound();
    assert.equal(head.robots(), 'noindex');
    assert.equal(head.canonical(), null);
    assert.equal(head.count(), 2, 'og:url and one robots tag, however often it is marked');
    router.pushRoute({ text: 'Genesis 1' }, { exclusive: true });
    assert.equal(head.robots(), null);
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/text/Genesis.1');
});

// ── history contract: seq, overlay markers, closeOverlay (audit U-2..U-5, U-12) ──

test('every entry carries seq: a push goes one deeper, a replace keeps the depth', async () => {
    const { router, window } = await loadRouter('', '/');
    router.pushRoute({ text: 'Genesis 1' }, { exclusive: true });
    assert.equal(window.history.state.seq, 1);
    router.pushRoute({ text: 'Genesis 2' }, { replace: true });
    assert.equal(window.history.state.seq, 1);
    router.pushRoute({ prayer: 'shacharit' }, { exclusive: true });
    assert.equal(window.history.state.seq, 2);
});

test('an overlay push is marked; a plain push or a replace never creates a marker', async () => {
    const { router, window } = await loadRouter('', '/text/Genesis.1');
    router.pushRoute({ conversation: 'c1', cv: 'overlay' }, { replace: true, overlay: 'conversation' });
    assert.equal(window.history.state.overlay, undefined, 'a replace is not an entry the app pushed');
    router.pushRoute({ conversation: null, cv: null }, { replace: true });
    router.pushRoute({ conversation: 'c1', cv: 'overlay' }, { overlay: 'conversation' });
    assert.deepEqual(window.history.calls.at(-1), ['push', {
        shelahRoute: { text: 'Genesis 1', conversation: 'c1', cv: 'overlay' }, overlay: 'conversation', pushedBy: 'app',
    }, '/text/Genesis.1/chat/c1']);
    router.pushRoute({ cv: 'full' }, { replace: true });
    assert.equal(window.history.state.overlay, 'conversation', 'a resize keeps the marker');
    router.pushRoute({ text: 'Exodus 1' }, { exclusive: true });
    assert.equal(window.history.state.overlay, undefined, 'a view push under the overlay is a plain entry');
});

test('a replace that removes the overlay drops its marker', async () => {
    const { router, window } = await loadRouter('', '/');
    router.pushRoute({ chat: 'h1' }, { exclusive: true, overlay: 'answer' });
    assert.equal(window.history.state.overlay, 'answer');
    router.pushRoute({ chat: null }, { replace: true });
    assert.equal(window.history.state.overlay, undefined);
});

test('closeOverlay steps Back off an entry the app pushed: history length unchanged, Back does not re-open', async () => {
    const { router, window } = await loadRouter('', '/text/Genesis.1');
    const seen = [];
    router.installRouter((route, detail) => seen.push([route, detail]));
    router.pushRoute({ conversation: 'c1', cv: 'overlay' }, { overlay: 'conversation' });
    const entriesBefore = window.history.entries.length;
    assert.equal(router.closeOverlay('conversation', { conversation: null, cv: null }), 'back');
    assert.equal(urlOf(window), '/text/Genesis.1');
    assert.equal(window.history.index, 0, 'on the entry under the overlay');
    assert.equal(window.history.entries.length, entriesBefore, 'nothing new was pushed');
    assert.deepEqual(seen.at(-1), [{ text: 'Genesis 1' }, { direction: 'back', saved: null, left: { text: 'Genesis 1', conversation: 'c1', cv: 'overlay' } }]);
});

test('closeOverlay replaces in place for a cold-loaded overlay (no entry of ours underneath)', async () => {
    const { router, window } = await loadRouter('?conversation=c1&cv=full', '/');
    router.installRouter(() => {});
    assert.equal(router.closeOverlay('conversation', { conversation: null, cv: null }), 'replace');
    assert.deepEqual(window.history.calls.at(-1), ['replace', { shelahRoute: {} }, '/']);
    assert.equal(window.history.entries.length, 1);
});

test('closeOverlay only steps Back for its own kind of overlay', async () => {
    const { router, window } = await loadRouter('', '/');
    router.pushRoute({ chat: 'h1' }, { exclusive: true, overlay: 'answer' });
    assert.equal(router.closeOverlay('conversation', { conversation: null, cv: null }), 'replace');
    assert.equal(window.history.calls.at(-1)[0], 'replace');
    assert.equal(router.closeOverlay('answer', { chat: null, a: null }), 'replace',
        'the replace above dropped nothing, but the answer marker is kept only while chat is set');
});

test('popstate reports direction by seq; the entry a visit started on counts as 0', async () => {
    const { router, window } = await loadRouter('', '/');
    const seen = [];
    router.installRouter((_route, detail) => seen.push(detail.direction));
    router.pushRoute({ text: 'Genesis 1' }, { exclusive: true });
    router.pushRoute({ text: 'Genesis 2' }, { exclusive: true });
    window.dispatch('popstate', { state: { shelahRoute: { text: 'Genesis 1' }, seq: 1 } });
    window.dispatch('popstate', { state: { shelahRoute: { text: 'Genesis 2' }, seq: 2 } });
    window.dispatch('popstate', { state: null });
    window.dispatch('popstate', { state: { shelahRoute: {} } });
    assert.deepEqual(seen, ['back', 'forward', 'back', null]);
});

test('clearRoute applies its patch in the same single write, and pushes on request', async () => {
    const { router, window } = await loadRouter('?layout=hebrew&vowels=1&lang=he&conversation=c1', '/text/Genesis.1');
    const next = router.clearRoute({ replace: false, keep: ['conversation', 'cv'], patch: { layout: null, vowels: null } });
    assert.deepEqual(next, { lang: 'he', conversation: 'c1', cv: 'overlay' });
    assert.equal(window.history.calls.length, 1, 'one write');
    assert.deepEqual(window.history.calls[0], ['push', { shelahRoute: next }, '/chat/c1?lang=he']);
    router.clearRoute({ replace: false, keep: ['conversation', 'cv'], patch: { layout: null } });
    assert.equal(window.history.calls.at(-1)[0], 'replace', 'already home: no second Home entry');
});

test('a literal + in a text or prayer path reads as a space', async () => {
    const { router } = await loadRouter('', '/');
    assert.deepEqual(router.parsePath('/text/Genesis+1'), { text: 'Genesis 1' });
    assert.deepEqual(router.parsePath('/text/Shulchan+Arukh,+Orach+Chayim.1.1'), { text: 'Shulchan Arukh, Orach Chayim 1:1' });
    assert.deepEqual(router.parsePath('/prayer/Upon+Arising'), { prayer: 'Upon Arising' });
    assert.deepEqual(router.parsePath('/text/Genesis%2B1'), { text: 'Genesis 1' });
});

test('a conversation over the home page is private: no canonical', async () => {
    const { router, head } = await loadRouterWithHead('', '/', 'https://shelah-app.vercel.app/');
    router.pushRoute({ conversation: 'c1' }, { overlay: 'conversation' });
    assert.equal(head.canonical(), null);
    router.pushRoute({ text: 'Genesis 1' }, { exclusive: true });
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/text/Genesis.1', 'over a text it is the text');
});

// ── U-7: what the page keeps on an entry ───────────────────────────────

test('annotateEntry merges into the entry on screen without a new entry or URL change', async () => {
    const { router, window } = await loadRouter('', '/text/Genesis.1');
    router.installRouter(() => {});
    const before = window.history.calls.length;
    router.annotateEntry({ viewScroll: { view: 'text', top: 120 } });
    router.annotateEntry({ other: 1 });
    router.annotateEntry(null);
    assert.equal(window.history.calls.length, before + 2, 'null writes nothing');
    assert.ok(window.history.calls.slice(before).every((call) => call[0] === 'replace'));
    assert.equal(urlOf(window), '/text/Genesis.1');
    assert.deepEqual(window.history.state.saved, { viewScroll: { view: 'text', top: 120 }, other: 1 });
});

test('Back hands the handler what was saved on the entry it returns to; a replace keeps it', async () => {
    const { router, window } = await loadRouter('', '/');
    const seen = [];
    router.installRouter((route, detail) => seen.push([route.text, detail.saved, detail.left?.text]));
    router.pushRoute({ text: 'Genesis 1' }, { exclusive: true });
    router.annotateEntry({ viewScroll: { view: 'text', top: 480 } });
    router.pushRoute({ text: 'Genesis 1', layout: 'hebrew' }, { exclusive: true, replace: true });
    assert.deepEqual(window.history.state.saved, { viewScroll: { view: 'text', top: 480 } },
        'a display-key replace keeps where the reader was');
    router.pushRoute({ text: 'Genesis 2' }, { exclusive: true });
    assert.equal(window.history.state.saved, undefined, 'a push starts clean');
    window.history.back();
    assert.deepEqual(seen, [['Genesis 1', { viewScroll: { view: 'text', top: 480 } }, 'Genesis 2']], 'and the route it left');
    window.history.back();
    assert.deepEqual(seen.at(-1), [undefined, null, 'Genesis 1']);
});

test('installRouter takes scroll restoration from the browser', async () => {
    const { router, window } = await loadRouter('', '/');
    window.history.scrollRestoration = 'auto';
    router.installRouter(() => {});
    assert.equal(window.history.scrollRestoration, 'manual');
});

test('an old ?community= link moves to its path once, with a replace (U-9)', async () => {
    const { router, window } = await loadRouter('?community=Spanish%20and%20Portuguese&lang=he', '/');
    router.installRouter(() => {});
    assert.equal(urlOf(window), '/community/Spanish_and_Portuguese?lang=he');
    assert.deepEqual(window.history.calls.map((call) => call[0]), ['replace']);
    assert.deepEqual(router.readRoute(), { community: 'Spanish and Portuguese', lang: 'he' });
});

// ── the history search in the URL (U-15) ───────────────────────────────

test('/history?q= reads the search; q means nothing on any other view (U-15)', async () => {
    const { router } = await loadRouter('?q=%20shabbat%20%20candles%20', '/history');
    assert.deepEqual(router.readRoute(), { history: '1', q: 'shabbat candles' });
    const other = await loadRouter('?q=shabbat', '/text/Genesis.1');
    assert.deepEqual(other.router.readRoute(), { text: 'Genesis 1' });
});

test('typing a search replaces /history?q=; clearing it drops q', async () => {
    const { router, window } = await loadRouter('', '/history');
    router.pushRoute({ q: 'kiddush' }, { replace: true });
    assert.equal(urlOf(window), '/history?q=kiddush');
    router.pushRoute({ q: null }, { replace: true });
    assert.equal(urlOf(window), '/history');
    assert.deepEqual(window.history.calls.map((call) => call[0]), ['replace', 'replace']);
});

test('q is capped at 200 characters and dropped by a switch to another view', async () => {
    const { router } = await loadRouter(`?q=${'a'.repeat(300)}`, '/history');
    assert.equal(router.readRoute().q.length, 200);
    const next = router.pushRoute({ text: 'Genesis 1' }, { exclusive: true });
    assert.deepEqual(next, { text: 'Genesis 1' });
});

test('/history?q= round-trips through routeUrl', async () => {
    const { router } = await loadRouter('', '/');
    const url = router.routeUrl({ history: '1', q: 'havdalah & wine' });
    assert.equal(url, '/history?q=havdalah+%26+wine');
    const back = await loadRouter(url.slice(url.indexOf('?')), '/history');
    assert.deepEqual(back.router.readRoute(), { history: '1', q: 'havdalah & wine' });
});

test('formal paths: the tail reads in any order and writes in one canonical order', async () => {
    const cases = [
        ['/chat/new', { conversation: 'new', cv: 'overlay' }],
        ['/chat/c1/all/balanced', { conversation: 'c1', cv: 'overlay', minhag: 'All', mode: 'balanced' }],
        ['/chat/c1/Greek-Romaniote/strict/full', { conversation: 'c1', cv: 'full', minhag: 'Greek-Romaniote', mode: 'strict' }],
        ['/answer/h1/turkish-ottoman/practical', { chat: 'h1', minhag: 'Turkish-Ottoman', mode: 'practical' }],
        [`/a/${SHARE_TOKEN}/sefardic`, { a: SHARE_TOKEN, minhag: 'Sefardic' }],
        ['/history/chat/c1/mini', { history: '1', conversation: 'c1', cv: 'mini' }],
        ['/text/Genesis.1/calendar/2026-09-25/chat/c1', { text: 'Genesis 1', date: '2026-09-25', conversation: 'c1', cv: 'overlay' }],
        ['/signin', { auth: 'signin' }],
        ['/profile', { auth: 'profile' }],
        ['/settings', { auth: 'settings' }],
        ['/text/Genesis.1/signin', { text: 'Genesis 1', auth: 'signin' }],
        ['/text/Genesis%2F1/chat/c1', { text: 'Genesis/1', conversation: 'c1', cv: 'overlay' }],
    ];
    for (const [pathname, route] of cases) {
        const { router } = await loadRouter('', pathname);
        assert.deepEqual(router.readRoute(), route, pathname);
    }
    const { router } = await loadRouter('', '/');
    assert.equal(router.routeUrl({ conversation: 'c1', cv: 'full', mode: 'strict', minhag: 'Greek-Romaniote', date: '2026-09-25', text: 'Genesis 1', auth: 'profile', lang: 'he' }),
        '/text/Genesis.1/chat/c1/greek-romaniote/strict/full/calendar/2026-09-25/profile?lang=he');
    const reordered = await loadRouter('', '/chat/c1/full/strict/sefardic');
    reordered.router.installRouter(() => {});
    assert.equal(urlOf(reordered.window), '/chat/c1/sefardic/strict/full', 'a reordered tail is rewritten once to the canonical order');
});

test('a community that spells a reserved word is dropped, not written as one', async () => {
    const { router } = await loadRouter('?conversation=c1&minhag=Strict&mode=balanced');
    assert.deepEqual(router.readRoute(), { conversation: 'c1', cv: 'overlay', mode: 'balanced' });
    assert.equal(router.routeUrl({ conversation: 'c1', minhag: 'Full' }), '/chat/c1');
});

test('the account modal is an overlay: pushed with a marker, closed by stepping back', async () => {
    const { router, window } = await loadRouter('', '/text/Genesis.1');
    router.pushRoute({ auth: 'signin' }, { overlay: 'auth' });
    assert.equal(urlOf(window), '/text/Genesis.1/signin');
    assert.equal(window.history.state.overlay, 'auth');
    assert.equal(router.closeOverlay('auth', { auth: null }), 'back');
    const cold = await loadRouter('', '/profile');
    assert.equal(cold.router.closeOverlay('auth', { auth: null }), 'replace');
    assert.equal(urlOf(cold.window), '/');
    const bad = await loadRouter('?auth=admin');
    assert.deepEqual(bad.router.readRoute(), {});
});

test('a conversation over a view keeps that view as the canonical page', async () => {
    const { router, window, head } = await loadRouterWithHead('', '/text/Genesis.1/chat/c1/all/balanced', null);
    router.installRouter(() => {});
    router.pushRoute({ mode: 'strict' }, { replace: true });
    assert.equal(urlOf(window), '/text/Genesis.1/chat/c1/all/strict');
    assert.equal(head.canonical(), 'https://shelah-app.vercel.app/text/Genesis.1');
    router.pushRoute({ text: null });
    assert.equal(head.canonical(), null, 'the conversation alone over home is private');
});
