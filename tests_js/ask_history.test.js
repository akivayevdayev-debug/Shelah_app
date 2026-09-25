/**
 * static/js/ask-history.js -- the /history page (paged, searchable list of
 * stored answers with inline delete confirm) and shelf 'ask' entries keyed
 * by ask_history id (promotion of question-text entries, and opening an
 * entry: stored answer, legacy match + migration, or re-ask).
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const ID1 = '3f2b8c1e-9a4d-4e6f-8b1a-2c3d4e5f6a7b';
const ID2 = '00000000-1111-4222-8333-444444444444';
const ID3 = '00000000-1111-4222-8333-555555555555';

async function load() {
    const mod = await loadEsmModule('static/js/ask-history.js', {
        window: {},
        document: {},
        URLSearchParams,
    });
    return mod.namespace;
}

const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const helpers = {
    t: (en) => en,
    escapeHtml,
    formatDate: (iso) => `D(${iso.slice(0, 10)})`,
    answerHref: (id) => `/answer/${id}`,
    icons: { search: '<path d="s"/>', delete: '<path d="d"/>' },
};

function row(id, question, extra = {}) {
    return { id, question, answer: `answer to ${question}`, created_at: '2026-09-24T10:00:00+00:00', ...extra };
}

// ── pure helpers ──────────────────────────────────────────────────────

test('isHistoryId accepts uuids only; sameQuestion ignores case and spacing', async () => {
    const m = await load();
    assert.equal(m.isHistoryId(ID1), true);
    assert.equal(m.isHistoryId(` ${ID1.toUpperCase()} `), true);
    for (const bad of ['', null, 'entry-1', `${ID1}x`, 'May I carry?']) assert.equal(m.isHistoryId(bad), false);
    assert.equal(m.sameQuestion('  May I   CARRY? ', 'may i carry?'), true);
    assert.equal(m.sameQuestion('', ''), false);
    assert.equal(m.sameQuestion('a', 'b'), false);
});

test('promoteAskEntries points both shelf buckets at the stored answer', async () => {
    const m = await load();
    const shelf = {
        recent: [
            { type: 'ask', value: 'May I carry?', label: 'May I carry?' },
            { type: 'text', value: 'Genesis.1', label: 'Genesis 1' },
            { type: 'ask', value: 'Other question', label: 'Other question' },
        ],
        bookmarks: [{ type: 'ask', value: ' may i  carry? ' }],
        notes: { keep: 1 },
        tracks: { 'Daily Study': [] },
    };
    const next = m.promoteAskEntries(shelf, 'May I carry?', ID1);
    assert.deepEqual(next.recent[0], { type: 'ask', value: ID1, label: 'May I carry?' });
    assert.deepEqual(next.recent.slice(1), shelf.recent.slice(1));
    assert.deepEqual(next.bookmarks, [{ type: 'ask', value: ID1, label: 'May I carry?' }]);
    assert.equal(next.notes, shelf.notes);
    assert.equal(shelf.recent[0].value, 'May I carry?', 'input shelf is not mutated');
});

test('promoteAskEntries drops the duplicate a rewrite creates, and no-ops when nothing matches', async () => {
    const m = await load();
    const shelf = {
        recent: [
            { type: 'ask', value: 'May I carry?', label: 'May I carry?' },
            { type: 'ask', value: ID1, label: 'May I carry?' },
            { type: 'ask', value: ID2, label: 'Other' },
        ],
        bookmarks: 'not a list',
    };
    const next = m.promoteAskEntries(shelf, 'may i carry?', ID1);
    assert.deepEqual(next.recent.map((i) => i.value), [ID1, ID2]);
    assert.equal(next.bookmarks, 'not a list');

    assert.equal(m.promoteAskEntries({ recent: [{ type: 'ask', value: ID1 }], bookmarks: [] }, 'x', ID1), null);
    assert.equal(m.promoteAskEntries(shelf, 'May I carry?', 'not-a-uuid'), null);
    assert.equal(m.promoteAskEntries(shelf, '   ', ID1), null);
    assert.equal(m.promoteAskEntries(null, 'q', ID1), null);
    // A same-text entry of another type is not an ask entry.
    assert.equal(m.promoteAskEntries({ recent: [{ type: 'text', value: 'q' }] }, 'q', ID1), null);
});

test('resolveShelfAsk opens a stored answer by id when signed in', async () => {
    const m = await load();
    const calls = [];
    const item = row(ID1, 'May I carry?');
    const out = await m.resolveShelfAsk({ type: 'ask', value: ID1, label: 'May I carry?' }, {
        signedIn: true,
        fetchEntry: async (id) => { calls.push(id); return item; },
        searchHistory: async () => { throw new Error('not used'); },
    });
    assert.deepEqual(out, { item });
    assert.deepEqual(calls, [ID1]);
});

test('resolveShelfAsk re-asks by label when the id is gone or the reader is signed out', async () => {
    const m = await load();
    const entry = { type: 'ask', value: ID1, label: 'May I carry?' };
    assert.deepEqual(await m.resolveShelfAsk(entry, { signedIn: true, fetchEntry: async () => null }),
        { reask: 'May I carry?', staleId: ID1 });
    assert.deepEqual(await m.resolveShelfAsk(entry, { signedIn: true, fetchEntry: async () => { throw new Error('500'); } }),
        { reask: 'May I carry?' });
    let fetched = false;
    assert.deepEqual(await m.resolveShelfAsk(entry, { signedIn: false, fetchEntry: async () => { fetched = true; } }),
        { reask: 'May I carry?' });
    assert.equal(fetched, false);
});

test('resolveShelfAsk matches a legacy question entry to the stored answer and asks for migration', async () => {
    const m = await load();
    const queries = [];
    const match = row(ID2, 'May I carry?');
    const out = await m.resolveShelfAsk({ type: 'ask', value: '  may i carry? ', label: 'x' }, {
        signedIn: true,
        searchHistory: async (q) => {
            queries.push(q);
            return { items: [row(ID1, 'May I carry? on Yom Tov'), { ...row(ID3, 'May I carry?'), answer: '' }, match] };
        },
    });
    assert.deepEqual(out, { item: match, migrateTo: ID2 });
    assert.deepEqual(queries, ['may i carry?']);
});

test('resolveShelfAsk re-asks a legacy entry with no exact stored match', async () => {
    const m = await load();
    const opts = (items) => ({ signedIn: true, searchHistory: async () => ({ items }) });
    assert.deepEqual(await m.resolveShelfAsk({ value: 'q1' }, opts([row(ID1, 'q10')])), { reask: 'q1' });
    assert.deepEqual(await m.resolveShelfAsk({ value: 'q1' }, opts(null)), { reask: 'q1' });
    assert.deepEqual(await m.resolveShelfAsk({ value: 'q1' }, { signedIn: true, searchHistory: async () => { throw new Error('x'); } }),
        { reask: 'q1' });
    assert.deepEqual(await m.resolveShelfAsk({ value: '', label: 'from label' }, opts([])), { reask: 'from label' });
    assert.deepEqual(await m.resolveShelfAsk({ value: ID1 }, { signedIn: false }), { reask: '' });
    assert.deepEqual(await m.resolveShelfAsk(null, { signedIn: true, searchHistory: async () => ({ items: [] }) }), { reask: '' });
});

test('historyQueryString', async () => {
    const m = await load();
    assert.equal(m.historyQueryString(), 'limit=20');
    assert.equal(m.historyQueryString({ q: '  shabbat & ', cursor: 'abc', limit: 5 }), 'limit=5&q=shabbat+%26&cursor=abc');
});

// ── rendering ─────────────────────────────────────────────────────────

function pageState(overrides = {}) {
    return { status: 'ready', items: [], nextCursor: null, q: '', loadingMore: false, confirmId: null, deleting: new Set(), ...overrides };
}

test('resultsHtml: skeleton while pending or on a first load', async () => {
    const m = await load();
    for (const status of ['pending', 'loading']) {
        const html = m.resultsHtml(pageState({ status }), helpers);
        assert.match(html, /aria-busy="true"/);
        assert.equal((html.match(/history-row--skeleton/g) || []).length, 6);
    }
});

test('resultsHtml: signed-out, unavailable, error and empty states', async () => {
    const m = await load();
    const signedOut = m.resultsHtml(pageState({ status: 'signed-out' }), helpers);
    assert.match(signedOut, /Sign in to see every question/);
    assert.match(signedOut, /data-action="sign-in"/);
    assert.doesNotMatch(m.resultsHtml(pageState({ status: 'unavailable' }), helpers), /data-action/);
    assert.match(m.resultsHtml(pageState({ status: 'error' }), helpers), /data-action="retry"/);
    assert.match(m.resultsHtml(pageState(), helpers), /No saved answers yet/);
    assert.match(m.resultsHtml(pageState({ q: '<b>x' }), helpers), /No questions match “&lt;b&gt;x”/);
});

test('resultsHtml: rows link to /answer/<id>, escape the question, and show the confirm group', async () => {
    const m = await load();
    const items = [row(ID1, '<img src=x onerror=alert(1)>'), row(ID2, '  ', { created_at: null })];
    let html = m.resultsHtml(pageState({ items }), helpers);
    assert.match(html, new RegExp(`href="/answer/${ID1}"`));
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(html, /<time class="history-row-date" datetime="2026-09-24T10:00:00\+00:00">D\(2026-09-24\)<\/time>/);
    assert.match(html, /Untitled question/);
    assert.match(html, /aria-label="Delete: &lt;img/);
    assert.equal((html.match(/data-action="delete"/g) || []).length, 2);

    html = m.resultsHtml(pageState({ items, confirmId: ID1 }), helpers);
    assert.match(html, /role="group" aria-label="Delete this answer\?"/);
    assert.match(html, new RegExp(`data-action="confirm-delete" data-id="${ID1}"`));
    assert.equal((html.match(/data-action="delete"/g) || []).length, 1);

    html = m.resultsHtml(pageState({ items, confirmId: ID1, deleting: new Set([ID1]) }), helpers);
    assert.match(html, /data-action="confirm-delete"[^>]*\n?\s*disabled aria-busy="true"/);
});

test('moreHtml: load more, busy, retry, and nothing on the last page', async () => {
    const m = await load();
    const items = [row(ID1, 'q')];
    assert.equal(m.moreHtml(pageState(), helpers), '');
    assert.equal(m.moreHtml(pageState({ items }), helpers), '');
    assert.match(m.moreHtml(pageState({ items, nextCursor: 'c' }), helpers), />Load more</);
    assert.match(m.moreHtml(pageState({ items, nextCursor: 'c', loadingMore: true }), helpers), /aria-busy="true" disabled>Loading…/);
    assert.match(m.moreHtml(pageState({ items, status: 'error' }), helpers), /try again/);
});

// ── controller ────────────────────────────────────────────────────────

function fakeEl() {
    const listeners = {};
    return {
        innerHTML: '',
        textContent: '',
        value: '',
        disabled: false,
        focused: 0,
        focus() { this.focused++; },
        addEventListener(type, fn) { (listeners[type] ||= new Set()).add(fn); },
        removeEventListener(type, fn) { listeners[type]?.delete(fn); },
        emit(type, event = {}) { listeners[type]?.forEach((fn) => fn(event)); },
        listenerCount(type) { return listeners[type]?.size || 0; },
    };
}

function fakeRoot() {
    const root = fakeEl();
    const parts = {
        '#historySearchInput': fakeEl(),
        '[data-history-results]': fakeEl(),
        '[data-history-more]': fakeEl(),
        '[data-history-status]': fakeEl(),
        '[data-history-search]': fakeEl(),
    };
    root.focusLog = [];
    root.contains = () => true;
    root.querySelector = (selector) => {
        if (parts[selector]) return parts[selector];
        const m = /^\[data-action="([\w-]+)"\]\[data-id="([^"]+)"\]$/.exec(selector);
        const html = parts['[data-history-results]'].innerHTML;
        if (m && html.includes(`data-action="${m[1]}" data-id="${m[2]}"`)) {
            return { focus: () => root.focusLog.push(`${m[1]}:${m[2]}`) };
        }
        return null;
    };
    root.parts = parts;
    return root;
}

function clickEvent(action, id, extra = {}) {
    const target = { getAttribute: (name) => (name === 'data-action' ? action : id) };
    let prevented = false;
    return {
        event: { target: { closest: () => target }, button: 0, preventDefault() { prevented = true; }, ...extra },
        prevented: () => prevented,
    };
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

const flush = () => new Promise((r) => setImmediate(r));

function controllerDeps(overrides = {}) {
    const timers = [];
    const deps = {
        ...helpers,
        auth: 'signed-in',
        authState: () => deps.auth,
        pages: [],
        fetchCalls: [],
        fetchPage(args) {
            deps.fetchCalls.push(args);
            const next = deps.pages.shift();
            return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
        },
        deleteEntry: async () => true,
        opened: [],
        openEntry: (item) => deps.opened.push(item.id),
        signIns: 0,
        signIn: () => { deps.signIns++; },
        deleted: [],
        onDeleted: (id) => deps.deleted.push(id),
        timers,
        setTimeout: (fn, ms) => { timers.push({ fn, ms, cleared: false }); return timers.length; },
        clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
        ...overrides,
    };
    return deps;
}

test('opening signed in loads the first page with a skeleton, then rows and Load more', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    const first = deferred();
    deps.fetchPage = (args) => { deps.fetchCalls.push(args); return first.promise; };
    const page = m.createHistoryPage(root, deps);
    assert.match(root.innerHTML, /role="search"/);
    assert.match(root.parts['[data-history-results]'].innerHTML, /history-row--skeleton/);
    assert.deepEqual(deps.fetchCalls, [{ q: '', cursor: '' }]);

    first.resolve({ items: [row(ID1, 'q1'), { id: 'bogus', question: 'dropped' }], next_cursor: 'c1' });
    await flush();
    assert.equal(page.state.status, 'ready');
    assert.deepEqual(page.state.items.map((i) => i.id), [ID1]);
    assert.match(root.parts['[data-history-more]'].innerHTML, /Load more/);
    assert.equal(root.parts['#historySearchInput'].disabled, false);
});

test('Load more appends the next page by cursor, skipping rows already shown', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    deps.pages.push({ items: [row(ID1, 'q1')], next_cursor: 'c1' }, { items: [row(ID1, 'q1'), row(ID2, 'q2')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();
    root.emit('click', clickEvent('load-more').event);
    assert.equal(page.state.loadingMore, true);
    await flush();
    assert.deepEqual(deps.fetchCalls[1], { q: '', cursor: 'c1' });
    assert.deepEqual(page.state.items.map((i) => i.id), [ID1, ID2]);
    assert.equal(root.parts['[data-history-more]'].innerHTML, '');
    assert.equal(root.parts['[data-history-status]'].textContent, '2 more loaded.');
});

test('a failed Load more keeps the rows and offers a retry that uses the same cursor', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    deps.pages.push({ items: [row(ID1, 'q1')], next_cursor: 'c1' }, new Error('502'), { items: [row(ID2, 'q2')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();
    root.emit('click', clickEvent('load-more').event);
    await flush();
    assert.equal(page.state.status, 'error');
    assert.match(root.parts['[data-history-results]'].innerHTML, new RegExp(ID1));
    assert.match(root.parts['[data-history-more]'].innerHTML, /try again/);
    root.emit('click', clickEvent('load-more').event);
    await flush();
    assert.deepEqual(deps.fetchCalls.map((c) => c.cursor), ['', 'c1', 'c1']);
    assert.deepEqual(page.state.items.map((i) => i.id), [ID1, ID2]);
});

test('typing searches after the debounce; a stale response never overwrites a newer one', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    deps.pages.push({ items: [row(ID1, 'shabbat q'), row(ID2, 'other')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();

    const slow = deferred();
    const fast = deferred();
    const queue = [slow, fast];
    deps.fetchPage = (args) => { deps.fetchCalls.push(args); return queue.shift().promise; };

    const input = root.parts['#historySearchInput'];
    input.value = 'shab';
    input.emit('input');
    input.value = '  shabbat   q ';
    input.emit('input');
    assert.equal(deps.timers.length, 2);
    assert.equal(deps.timers[0].cleared, true);
    assert.equal(deps.timers[1].ms, m.SEARCH_DEBOUNCE_MS);
    deps.timers[1].fn();
    assert.deepEqual(deps.fetchCalls.at(-1), { q: 'shabbat q', cursor: '' });

    // A submit (Enter) supersedes the in-flight debounced search at once.
    input.value = 'other';
    root.parts['[data-history-search]'].emit('submit', { preventDefault() {} });
    assert.deepEqual(deps.fetchCalls.at(-1), { q: 'other', cursor: '' });

    fast.resolve({ items: [row(ID2, 'other')], next_cursor: null });
    await flush();
    slow.resolve({ items: [row(ID1, 'shabbat q')], next_cursor: null });
    await flush();
    assert.equal(page.state.q, 'other');
    assert.deepEqual(page.state.items.map((i) => i.id), [ID2]);
    assert.equal(root.parts['[data-history-status]'].textContent, '1 matching questions.');
});

test('an unchanged query does not refetch; no matches is announced', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    deps.pages.push({ items: [], next_cursor: null }, { items: [], next_cursor: null });
    m.createHistoryPage(root, deps);
    await flush();
    const input = root.parts['#historySearchInput'];
    input.value = '   ';
    input.emit('input');
    deps.timers.at(-1).fn();
    assert.equal(deps.fetchCalls.length, 1);
    input.value = 'zzz';
    input.emit('input');
    deps.timers.at(-1).fn();
    await flush();
    assert.equal(root.parts['[data-history-status]'].textContent, 'No matching questions.');
    assert.match(root.parts['[data-history-results]'].innerHTML, /No questions match “zzz”/);
});

test('a plain click on a row opens the answer in-app; a modified click is left to the browser', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    deps.pages.push({ items: [row(ID1, 'q1')], next_cursor: null });
    m.createHistoryPage(root, deps);
    await flush();

    const plain = clickEvent('open', ID1);
    root.emit('click', plain.event);
    assert.equal(plain.prevented(), true);
    assert.deepEqual(deps.opened, [ID1]);

    for (const mod of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
        const click = clickEvent('open', ID1, mod);
        root.emit('click', click.event);
        assert.equal(click.prevented(), false);
    }
    const unknown = clickEvent('open', ID2);
    root.emit('click', unknown.event);
    assert.equal(unknown.prevented(), false);
    assert.deepEqual(deps.opened, [ID1]);

    root.emit('click', { target: { closest: () => null } });
    root.emit('click', { target: {} });
});

test('delete asks first; Cancel and Escape back out with focus returned to the delete button', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    let deletes = 0;
    deps.deleteEntry = async () => { deletes++; return true; };
    deps.pages.push({ items: [row(ID1, 'q1'), row(ID2, 'q2')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();

    root.emit('click', clickEvent('delete', ID1).event);
    assert.equal(page.state.confirmId, ID1);
    assert.deepEqual(root.focusLog, [`cancel-delete:${ID1}`]);
    root.emit('click', clickEvent('cancel-delete', ID1).event);
    assert.equal(page.state.confirmId, null);
    assert.deepEqual(root.focusLog.at(-1), `delete:${ID1}`);

    root.emit('click', clickEvent('delete', ID2).event);
    let prevented = false;
    root.emit('keydown', { key: 'Escape', preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(page.state.confirmId, null);
    assert.deepEqual(root.focusLog.at(-1), `delete:${ID2}`);
    root.emit('keydown', { key: 'Escape' });
    root.emit('keydown', { key: 'Enter' });
    assert.equal(deletes, 0);
});

test('confirming deletes the row, announces it, refreshes other views and moves focus to the next row', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    const pending = deferred();
    deps.deleteEntry = () => pending.promise;
    deps.pages.push({ items: [row(ID1, 'q1'), row(ID2, 'q2'), row(ID3, 'q3')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();

    root.emit('click', clickEvent('delete', ID2).event);
    root.emit('click', clickEvent('confirm-delete', ID2).event);
    assert.match(root.parts['[data-history-results]'].innerHTML, /disabled aria-busy="true"/);
    root.emit('click', clickEvent('confirm-delete', ID2).event); // double click is ignored
    pending.resolve(true);
    await flush();
    assert.deepEqual(page.state.items.map((i) => i.id), [ID1, ID3]);
    assert.equal(root.parts['[data-history-status]'].textContent, 'Answer deleted.');
    assert.deepEqual(deps.deleted, [ID2]);
    assert.equal(root.focusLog.at(-1), `open:${ID3}`);

    // Last row: focus goes to the previous one; the only row: the search box.
    deps.deleteEntry = async () => true;
    root.emit('click', clickEvent('confirm-delete', ID3).event);
    await flush();
    assert.equal(root.focusLog.at(-1), `open:${ID1}`);
    root.emit('click', clickEvent('confirm-delete', ID1).event);
    await flush();
    assert.equal(root.parts['#historySearchInput'].focused, 1);
    assert.match(root.parts['[data-history-results]'].innerHTML, /No saved answers yet/);
});

test('a failed delete keeps the row and the confirm, and says so', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    deps.pages.push({ items: [row(ID1, 'q1')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();
    for (const failing of [async () => false, async () => { throw new Error('net'); }]) {
        deps.deleteEntry = failing;
        root.emit('click', clickEvent('delete', ID1).event);
        root.emit('click', clickEvent('confirm-delete', ID1).event);
        await flush();
        assert.deepEqual(page.state.items.map((i) => i.id), [ID1]);
        assert.equal(page.state.confirmId, ID1);
        assert.match(root.parts['[data-history-status]'].textContent, /Couldn't delete/);
        assert.equal(root.focusLog.at(-1), `confirm-delete:${ID1}`);
    }
    assert.deepEqual(deps.deleted, []);
});

test('signed out shows the sign-in CTA without fetching; signing in loads the list', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps({ auth: 'signed-out' });
    const page = m.createHistoryPage(root, deps);
    assert.equal(deps.fetchCalls.length, 0);
    assert.match(root.parts['[data-history-results]'].innerHTML, /data-action="sign-in"/);
    assert.equal(root.parts['#historySearchInput'].disabled, true);
    root.emit('click', clickEvent('sign-in').event);
    assert.equal(deps.signIns, 1);

    deps.auth = 'signed-in';
    deps.pages.push({ items: [row(ID1, 'q1')], next_cursor: null });
    page.refresh();
    await flush();
    assert.deepEqual(page.state.items.map((i) => i.id), [ID1]);

    // Signing out while the page shows clears the list at once.
    deps.auth = 'signed-out';
    page.refresh();
    assert.deepEqual(page.state.items, []);
    assert.equal(page.state.status, 'signed-out');
});

test('pending auth shows the skeleton; unavailable auth explains; a load failure offers retry', async () => {
    const m = await load();
    let root = fakeRoot();
    let deps = controllerDeps({ auth: 'pending' });
    m.createHistoryPage(root, deps);
    assert.match(root.parts['[data-history-results]'].innerHTML, /history-row--skeleton/);

    root = fakeRoot();
    deps = controllerDeps({ auth: 'unavailable' });
    m.createHistoryPage(root, deps);
    assert.match(root.parts['[data-history-results]'].innerHTML, /isn't available here/);

    root = fakeRoot();
    deps = controllerDeps();
    deps.pages.push(new Error('500'), { items: [row(ID1, 'q1')], next_cursor: null });
    const page = m.createHistoryPage(root, deps);
    await flush();
    assert.match(root.parts['[data-history-results]'].innerHTML, /data-action="retry"/);
    assert.match(root.parts['[data-history-status]'].textContent, /Couldn't load/);
    root.emit('click', clickEvent('retry').event);
    await flush();
    assert.deepEqual(page.state.items.map((i) => i.id), [ID1]);
});

test('destroy detaches every listener and ignores responses still in flight', async () => {
    const m = await load();
    const root = fakeRoot();
    const deps = controllerDeps();
    const first = deferred();
    deps.fetchPage = () => first.promise;
    const page = m.createHistoryPage(root, deps);
    const input = root.parts['#historySearchInput'];
    input.value = 'x';
    input.emit('input');
    page.destroy();
    assert.equal(deps.timers[0].cleared, true);
    assert.equal(root.listenerCount('click'), 0);
    assert.equal(root.listenerCount('keydown'), 0);
    assert.equal(input.listenerCount('input'), 0);
    assert.equal(root.parts['[data-history-search]'].listenerCount('submit'), 0);
    const before = root.parts['[data-history-results]'].innerHTML;
    first.resolve({ items: [row(ID1, 'q1')], next_cursor: null });
    await flush();
    assert.equal(root.parts['[data-history-results]'].innerHTML, before);
    page.refresh();
    assert.equal(root.parts['[data-history-results]'].innerHTML, before);
});
