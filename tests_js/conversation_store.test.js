/**
 * static/js/conversation-store.js -- the conversation view-model every UI
 * surface (overlay, mini widget, full page, mobile sheet/PiP) renders from.
 *
 * Pins the behaviors the backend contract depends on: the minhag lock (the
 * row is created lazily on the first send with the draft community, and is
 * locked from then on), optimistic turns replaced by the server's, the
 * sources-disclosure phases reflecting REAL stored citations, failure turns
 * that are retryable, and stale async results never landing in a thread the
 * user has since left.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const STORE_PATH = 'static/js/conversation-store.js';

async function loadStore() {
    const mod = await loadEsmModule(STORE_PATH, {
        window: {}, fetch: async () => { throw new Error('real fetch must not be used'); },
        setTimeout, clearTimeout, AbortController,
    });
    return mod.namespace;
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function apiError(code, extra = {}) {
    return Object.assign(new Error(code), { code, ...extra });
}

// A fake conversation-api with call recording; override any method per test.
function makeApi(overrides = {}) {
    const calls = [];
    const record = (name, fn) => async (...args) => {
        calls.push([name, ...args]);
        return fn(...args);
    };
    const api = {
        ERROR_CODES: { NETWORK: 'network', ANSWER_INCOMPLETE: 'answer_incomplete' },
        createConversation: record('create', async ({ minhag }) => ({ id: 'c1', title: '', minhag })),
        askInConversation: record('ask', async (id, { question }) => ({
            user_message: { id: 'u1', role: 'user', content: question, status: 'complete' },
            assistant_message: {
                id: 'a1', role: 'assistant', content: 'Answer.', status: 'complete',
                citations: [
                    { ordinal: 1, source_ref: 'Yoreh De\'ah 95:3', excerpt_en: 'irui', url: 'https://www.sefaria.org/x' },
                    { ordinal: 0, source_ref: 'Yalkut Yosef', url: 'javascript:alert(1)' },
                ],
            },
            conversation: { id, title: question, minhag: 'Sefardic' },
        })),
        getConversation: record('get', async (id) => ({ id, title: 'T', minhag: 'Ashkenaz', messages: [] })),
        listConversations: record('list', async () => []),
        renameConversation: record('rename', async (id, title) => ({ id, title, title_is_custom: true })),
        setConversationPinned: record('pin', async (id, pinned) => ({ id, pinned_at: pinned ? '2026-09-23T00:00:00Z' : null })),
        deleteConversation: record('delete', async () => ({ ok: true })),
        restoreConversation: record('restore', async (id) => ({ id, title: 'Restored', updated_at: '2026-09-10T00:00:00Z' })),
    };
    Object.assign(api, Object.fromEntries(Object.entries(overrides).map(([k, fn]) => [k, record(k, fn)])));
    api.calls = calls;
    return api;
}

test('first send creates the conversation with the draft minhag, then locks it', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi();
    const store = createConversationStore({ api, getPrefs: () => ({ community: 'Sefardic', mode: 'practical', language: 'he' }) });

    assert.equal(store.getState().minhagLocked, false);
    assert.equal(store.getState().draftMinhag, 'Sefardic');
    assert.equal(store.setDraftMinhag('Ashkenaz'), true, 'editable before the first send');

    assert.equal(await store.send('  Dishwasher?  '), true);

    const create = api.calls.find((c) => c[0] === 'create');
    assert.deepEqual(create[1], { minhag: 'Ashkenaz' });
    const ask = api.calls.find((c) => c[0] === 'ask');
    assert.deepEqual(ask.slice(1, 3), ['c1', { question: 'Dishwasher?', mode: 'practical', language: 'he' }]);

    const state = store.getState();
    assert.equal(state.minhagLocked, true);
    assert.equal(store.setDraftMinhag('Persian'), false, 'locked once the thread exists');
    assert.equal(state.conversation.title, 'Dishwasher?', 'header comes from the ask response');
    assert.deepEqual(state.list.items.map((i) => i.id), ['c1'], 'new thread appears in the sidebar list');
});

test('"All" draft community is created as a null minhag', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi();
    const store = createConversationStore({ api, getPrefs: () => ({ community: 'All' }) });
    await store.send('q');
    assert.deepEqual(api.calls.find((c) => c[0] === 'create')[1], { minhag: null });
});

test('later sends reuse the conversation and pass only server message ids as known', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi();
    const store = createConversationStore({ api });
    await store.send('first');
    await store.send('second');
    assert.equal(api.calls.filter((c) => c[0] === 'create').length, 1);
    const secondAsk = api.calls.filter((c) => c[0] === 'ask')[1];
    assert.deepEqual(secondAsk[3].knownMessageIds, ['u1', 'a1']);
});

test('Standard/Deep choices map to backend answer modes', async () => {
    const { createConversationStore, ANSWER_MODE_BY_CHOICE } = await loadStore();
    assert.deepEqual({ ...ANSWER_MODE_BY_CHOICE }, { standard: 'balanced', deep: 'sources' });
    const api = makeApi();
    const store = createConversationStore({ api });
    await store.send('q', { mode: 'deep' });
    assert.equal(api.calls.find((c) => c[0] === 'ask')[2].mode, 'sources');
});

test('while answering: optimistic pending turns + sources "searching"; after: server turns + real citations', async () => {
    const { createConversationStore, SOURCES_PHASE, MESSAGE_STATUS } = await loadStore();
    const gate = deferred();
    const base = makeApi();
    const api = makeApi({ askInConversation: async (...args) => { await gate.promise; return base.askInConversation(...args); } });
    const store = createConversationStore({ api });

    const sending = store.send('Q?');
    await new Promise((r) => setImmediate(r));
    let state = store.getState();
    assert.equal(state.sending, true);
    assert.deepEqual(state.messages.map((m) => [m.role, m.status]), [
        ['user', MESSAGE_STATUS.PENDING], ['assistant', MESSAGE_STATUS.PENDING],
    ]);
    assert.equal(state.sources.phase, SOURCES_PHASE.SEARCHING);
    assert.deepEqual(state.sources.citations, [], 'no invented source names while waiting');
    assert.equal(await store.send('again'), false, 'one send at a time');

    gate.resolve();
    await sending;
    state = store.getState();
    assert.equal(state.sending, false);
    assert.deepEqual(state.messages.map((m) => m.id), ['u1', 'a1']);
    assert.equal(state.sources.phase, SOURCES_PHASE.DONE);
    assert.equal(state.sources.messageId, 'a1');
    assert.deepEqual(state.sources.citations.map((c) => [c.ordinal, c.ref, c.url]), [
        [0, 'Yalkut Yosef', null],
        [1, 'Yoreh De\'ah 95:3', 'https://www.sefaria.org/x'],
    ], 'sorted by ordinal; unsafe URLs dropped');
});

test('a stored error answer is kept as a retryable turn and sources read "unavailable"', async () => {
    const { createConversationStore, SOURCES_PHASE } = await loadStore();
    const api = makeApi({
        askInConversation: async (id, { question }) => ({
            user_message: { id: 'u1', role: 'user', content: question },
            assistant_message: { id: 'e1', role: 'assistant', content: '', status: 'error', citations: [] },
            conversation: { id },
        }),
    });
    const store = createConversationStore({ api });
    await store.send('Q?');
    const state = store.getState();
    assert.deepEqual(state.messages.map((m) => [m.id, m.status]), [['u1', 'complete'], ['e1', 'error']]);
    assert.equal(state.sources.phase, SOURCES_PHASE.UNAVAILABLE);
});

test('a client-only error placeholder (no id) still gets a stable id', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({
        askInConversation: async (id, { question }) => ({
            user_message: { id: 'u1', role: 'user', content: question },
            assistant_message: { role: 'assistant', content: '', status: 'error', citations: [] },
        }),
    });
    const store = createConversationStore({ api });
    await store.send('Q?');
    assert.ok(store.getState().messages[1].id.startsWith('local-'));
});

test('network failure: question stays visible as FAILED, retry re-sends it', async () => {
    const { createConversationStore, MESSAGE_STATUS } = await loadStore();
    let fail = true;
    const base = makeApi();
    const api = makeApi({
        askInConversation: async (...args) => {
            if (fail) throw apiError('network');
            return base.askInConversation(...args);
        },
    });
    const store = createConversationStore({ api });

    assert.equal(await store.send('Q?'), false);
    let state = store.getState();
    assert.equal(state.lastError.code, 'network');
    assert.equal(state.messages.length, 1);
    assert.equal(state.messages[0].status, MESSAGE_STATUS.FAILED);

    fail = false;
    assert.equal(await store.retry(state.messages[0].id), true);
    state = store.getState();
    assert.deepEqual(state.messages.map((m) => m.id), ['u1', 'a1']);
    assert.equal(state.lastError, null);
});

test('answer_incomplete: shows the saved question plus an INCOMPLETE answer slot', async () => {
    const { createConversationStore, MESSAGE_STATUS } = await loadStore();
    const api = makeApi({
        askInConversation: async () => {
            throw apiError('answer_incomplete', { userMessage: { id: 'u9', role: 'user', content: 'Q?' } });
        },
    });
    const store = createConversationStore({ api });
    await store.send('Q?');
    const state = store.getState();
    assert.deepEqual(state.messages.map((m) => [m.id.startsWith('local-') ? 'local' : m.id, m.status]), [
        ['u9', 'complete'], ['local', MESSAGE_STATUS.INCOMPLETE],
    ]);
});

test('retrying a server error turn re-asks the question before it', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({
        getConversation: async (id) => ({
            id, minhag: null, messages: [
                { id: 'u1', role: 'user', content: 'Original?' },
                { id: 'e1', role: 'assistant', content: '', status: 'error' },
            ],
        }),
    });
    const store = createConversationStore({ api });
    await store.open('c7');
    await store.retry('e1');
    assert.equal(api.calls.filter((c) => c[0] === 'ask').at(-1)[2].question, 'Original?');
    assert.equal(api.calls.filter((c) => c[0] === 'create').length, 0, 'existing thread is reused');
});

test('open() hydrates a thread, locks its minhag, and shows the last answer\'s sources', async () => {
    const { createConversationStore, SOURCES_PHASE, minhagLabel } = await loadStore();
    const api = makeApi({
        getConversation: async (id) => ({
            id, title: 'Candles', minhag: 'Ashkenaz', messages: [
                { id: 'u1', role: 'user', content: 'q' },
                { id: 'a1', role: 'assistant', content: 'a', citations: [{ ordinal: 0, source_ref: 'OC 263' }] },
            ],
        }),
    });
    const store = createConversationStore({ api });
    const seen = [];
    store.subscribe((s) => seen.push(s.loadStatus));

    assert.equal(await store.open('c2'), true);
    const state = store.getState();
    assert.deepEqual(seen.slice(0, 1), ['loading']);
    assert.equal(state.loadStatus, 'ready');
    assert.equal(state.minhagLocked, true);
    assert.equal(minhagLabel(state.conversation.minhag), 'Ashkenaz');
    assert.equal(state.sources.phase, SOURCES_PHASE.DONE);
    assert.deepEqual(state.sources.citations.map((c) => c.ref), ['OC 263']);
});

test('open() failure reports a load error code', async () => {
    const { createConversationStore } = await loadStore();
    const store = createConversationStore({ api: makeApi({ getConversation: async () => { throw apiError('not_found'); } }) });
    assert.equal(await store.open('gone'), false);
    assert.equal(store.getState().loadStatus, 'error');
    assert.equal(store.getState().loadError.code, 'not_found');
});

test('a slow answer never lands in a thread the user has since left', async () => {
    const { createConversationStore } = await loadStore();
    const gate = deferred();
    const base = makeApi();
    const api = makeApi({ askInConversation: async (...args) => { await gate.promise; return base.askInConversation(...args); } });
    const store = createConversationStore({ api });

    const sending = store.send('Q?');
    await new Promise((r) => setImmediate(r));
    store.startNew({ minhag: 'Persian' });
    gate.resolve();
    assert.equal(await sending, false);

    const state = store.getState();
    assert.deepEqual(state.messages, []);
    assert.equal(state.conversation, null);
    assert.equal(state.draftMinhag, 'Persian');
    assert.equal(state.minhagLocked, false);
    assert.equal(state.sending, false);
});

test('sidebar list: pinned first, then most recent; rename/pin/delete keep it in sync', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({
        listConversations: async () => [
            { id: 'old', updated_at: '2026-09-01T00:00:00Z' },
            { id: 'new', updated_at: '2026-09-20T00:00:00Z' },
            { id: 'pinned', pinned_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' },
        ],
    });
    const store = createConversationStore({ api });

    await store.loadList();
    assert.deepEqual(store.getState().list.items.map((i) => i.id), ['pinned', 'new', 'old']);

    await store.setPinned('old', true);
    assert.equal(store.getState().list.items[0].id, 'old');

    await store.open('new');
    await store.rename('new', 'Renamed');
    assert.equal(store.getState().conversation.title, 'Renamed');
    assert.equal(store.getState().conversation.titleIsCustom, true);

    await store.remove('new');
    assert.ok(!store.getState().list.items.some((i) => i.id === 'new'));
    assert.equal(store.getState().conversation, null, 'deleting the open thread starts a fresh one');
});

test('failed rename surfaces lastError and leaves state untouched', async () => {
    const { createConversationStore } = await loadStore();
    const store = createConversationStore({ api: makeApi({ renameConversation: async () => { throw apiError('rate_limited', { retryAfter: 60 }); } }) });
    assert.equal(await store.rename('c1', 'x'), false);
    assert.deepEqual(store.getState().lastError, { code: 'rate_limited', message: 'rate_limited', retryAfter: 60 });
});

test('safeCitationUrl allows https and same-origin paths only', async () => {
    const { safeCitationUrl } = await loadStore();
    assert.equal(safeCitationUrl('https://www.sefaria.org/Berakhot.2a'), 'https://www.sefaria.org/Berakhot.2a');
    assert.equal(safeCitationUrl('/?text=Berakhot.2a'), '/?text=Berakhot.2a');
    for (const bad of ['http://x.org', 'javascript:alert(1)', 'data:text/html,x', '//evil.example', 'not a url', '']) {
        assert.equal(safeCitationUrl(bad), null, bad);
    }
});

test('a listener that throws does not stop other listeners', async () => {
    const { createConversationStore } = await loadStore();
    const store = createConversationStore({ api: makeApi() });
    let calls = 0;
    store.subscribe(() => { throw new Error('boom'); });
    const unsubscribe = store.subscribe(() => { calls += 1; });
    store.startNew();
    unsubscribe();
    store.startNew();
    assert.equal(calls, 1);
});

// ── retry of a stored error turn (retry_of) ─────────────────────────────

const STORED_ERROR_THREAD = (id) => ({
    id, title: 'T', minhag: null, messages: [
        { id: 'u0', role: 'user', content: 'Earlier?' },
        { id: 'a0', role: 'assistant', content: 'Earlier answer.', status: 'complete' },
        { id: 'u1', role: 'user', content: 'Original?' },
        { id: 'e1', role: 'assistant', content: '', status: 'error' },
    ],
});

test('retrying a stored error turn uses retryOf, keeps the user turn, and swaps in the new answer', async () => {
    const { createConversationStore, SOURCES_PHASE, MESSAGE_STATUS } = await loadStore();
    const gate = deferred();
    const api = makeApi({
        getConversation: async (id) => STORED_ERROR_THREAD(id),
        askInConversation: async (id) => {
            await gate.promise;
            return {
                user_message: { id: 'u1', role: 'user', content: 'Original?' },
                assistant_message: { id: 'a2', role: 'assistant', content: 'New.', status: 'complete', citations: [{ ordinal: 0, source_ref: 'OC 1' }] },
                conversation: { id, title: 'T', updated_at: '2026-09-23T00:00:00Z' },
                superseded_message_id: 'e1',
            };
        },
    });
    const store = createConversationStore({ api, getPrefs: () => ({ mode: 'practical', language: 'he' }) });
    await store.open('c7');

    const retrying = store.retry('e1', { mode: 'deep' });
    await new Promise((r) => setImmediate(r));
    let state = store.getState();
    assert.equal(state.sending, true);
    assert.deepEqual(state.messages.map((m) => (m.id.startsWith('local-') ? `local:${m.status}` : m.id)), [
        'u0', 'a0', 'u1', `local:${MESSAGE_STATUS.PENDING}`,
    ], 'user turn stays in place; only the answer slot is pending');
    assert.equal(state.sources.phase, SOURCES_PHASE.SEARCHING);
    assert.equal(await store.retry('e1'), false, 'one request at a time');

    gate.resolve();
    assert.equal(await retrying, true);
    const ask = api.calls.find((c) => c[0] === 'askInConversation');
    assert.deepEqual(ask.slice(1, 3), ['c7', { question: 'Original?', mode: 'sources', language: 'he', retryOf: 'e1' }]);
    assert.deepEqual(ask[3].knownMessageIds, ['u0', 'a0', 'u1', 'e1']);
    assert.equal(api.calls.filter((c) => c[0] === 'create').length, 0);

    state = store.getState();
    assert.equal(state.sending, false);
    assert.deepEqual(state.messages.map((m) => m.id), ['u0', 'a0', 'u1', 'a2']);
    assert.equal(state.sources.phase, SOURCES_PHASE.DONE);
    assert.equal(state.sources.messageId, 'a2');
    assert.deepEqual(state.sources.citations.map((c) => c.ref), ['OC 1']);
    assert.deepEqual(state.list.items.map((i) => i.id), ['c7'], 'header upserted into the sidebar');
});

test('a stored-error retry that comes back as another error turn keeps sources unavailable', async () => {
    const { createConversationStore, SOURCES_PHASE } = await loadStore();
    const api = makeApi({
        getConversation: async (id) => STORED_ERROR_THREAD(id),
        // No user_message / no assistant id / no conversation: fall back to what the store had.
        askInConversation: async () => ({ assistant_message: { role: 'assistant', status: 'error' } }),
    });
    const store = createConversationStore({ api });
    await store.open('c7');
    assert.equal(await store.retry('e1'), true);
    const state = store.getState();
    assert.equal(state.messages[2].id, 'u1', 'the existing user turn is kept when the body omits it');
    assert.ok(state.messages[3].id.startsWith('local-'));
    assert.equal(state.messages[3].status, 'error');
    assert.equal(state.sources.phase, SOURCES_PHASE.UNAVAILABLE);
    assert.equal(state.conversation.id, 'c7');
});

test('a failed stored-error retry (e.g. budget exhausted) puts the error turn back with lastError', async () => {
    const { createConversationStore, SOURCES_PHASE } = await loadStore();
    const api = makeApi({
        getConversation: async (id) => STORED_ERROR_THREAD(id),
        askInConversation: async () => { throw apiError('budget_exhausted'); },
    });
    const store = createConversationStore({ api });
    await store.open('c7');
    assert.equal(await store.retry('e1'), false);
    const state = store.getState();
    assert.equal(state.sending, false);
    assert.equal(state.lastError.code, 'budget_exhausted');
    assert.deepEqual(state.messages.map((m) => [m.id, m.status]).slice(2), [['u1', 'complete'], ['e1', 'error']]);
    assert.deepEqual(state.sources, { phase: SOURCES_PHASE.UNAVAILABLE, citations: [], messageId: 'e1' });
});

test('a stored-error retry never lands in a thread the user has since left', async () => {
    for (const outcome of ['resolve', 'reject']) {
        const { createConversationStore } = await loadStore();
        const gate = deferred();
        const api = makeApi({
            getConversation: async (id) => STORED_ERROR_THREAD(id),
            askInConversation: async () => {
                await gate.promise;
                if (outcome === 'reject') throw apiError('network');
                return { assistant_message: { id: 'a2', role: 'assistant', status: 'complete' } };
            },
        });
        const store = createConversationStore({ api });
        await store.open('c7');
        const retrying = store.retry('e1');
        await new Promise((r) => setImmediate(r));
        store.startNew();
        gate.resolve();
        assert.equal(await retrying, false, outcome);
        assert.deepEqual(store.getState().messages, [], outcome);
        assert.equal(store.getState().lastError, null, outcome);
    }
});

test('stored-error retry refuses when there is no question before it or no open conversation', async () => {
    const { createConversationStore } = await loadStore();
    const orphan = makeApi({
        getConversation: async (id) => ({ id, messages: [{ id: 'e1', role: 'assistant', status: 'error' }] }),
    });
    let store = createConversationStore({ api: orphan });
    await store.open('c7');
    assert.equal(await store.retry('e1'), false);

    // A thread body without an id normalizes to no conversation header.
    const headless = makeApi({ getConversation: async () => ({ messages: STORED_ERROR_THREAD('x').messages }) });
    store = createConversationStore({ api: headless });
    await store.open('c7');
    assert.equal(await store.retry('e1'), false);
    assert.equal(headless.calls.filter((c) => c[0] === 'ask').length, 0);
});

test('a stored error that is no longer the latest turn is not retried (its answer would land out of order)', async () => {
    const { createConversationStore } = await loadStore();
    const base = STORED_ERROR_THREAD('c7');
    const api = makeApi({
        getConversation: async (id) => ({
            ...base, id, messages: [...base.messages,
                { id: 'u2', role: 'user', content: 'Later?' },
                { id: 'a2', role: 'assistant', content: 'Later answer.', status: 'complete' }],
        }),
    });
    const store = createConversationStore({ api });
    await store.open('c7');
    assert.equal(await store.retry('e1'), false);
    assert.equal(api.calls.filter((c) => c[0] === 'askInConversation').length, 0);
    assert.equal(store.getState().messages.length, 6, 'thread left untouched');
});

test('an error placeholder the server never stored (local id) is retried as a fresh send', async () => {
    const { createConversationStore } = await loadStore();
    let first = true;
    const base = makeApi();
    const api = makeApi({
        askInConversation: async (...args) => {
            if (first) {
                first = false;
                return { user_message: { id: 'u1', role: 'user', content: 'Q?' }, assistant_message: { role: 'assistant', status: 'error' } };
            }
            return base.askInConversation(...args);
        },
    });
    const store = createConversationStore({ api });
    await store.send('Q?');
    const placeholder = store.getState().messages[1].id;
    assert.ok(placeholder.startsWith('local-'));
    assert.equal(await store.retry(placeholder), true);
    const asks = api.calls.filter((c) => c[0] === 'askInConversation');
    assert.equal(asks[1][2].retryOf, undefined, 'no retry_of for a turn the server never stored');
    assert.equal(asks[1][2].question, 'Q?');
});

test('an INCOMPLETE answer slot is retried as a fresh send of its question', async () => {
    const { createConversationStore } = await loadStore();
    let first = true;
    const base = makeApi();
    const api = makeApi({
        askInConversation: async (...args) => {
            if (first) {
                first = false;
                throw apiError('answer_incomplete', { userMessage: { id: 'u9', role: 'user', content: 'Q?' } });
            }
            return base.askInConversation(...args);
        },
    });
    const store = createConversationStore({ api });
    await store.send('Q?');
    const slot = store.getState().messages[1].id;
    assert.equal(await store.retry(slot), true);
    assert.equal(api.calls.filter((c) => c[0] === 'askInConversation')[1][2].retryOf, undefined);
    assert.deepEqual(store.getState().messages.map((m) => m.id), ['u1', 'a1']);
});

// ── cost guards on send ─────────────────────────────────────────────────

test('budget_exhausted / ai_paused on send: lastError carries the code (+retryAfter), question stays FAILED', async () => {
    const { createConversationStore, MESSAGE_STATUS, SOURCES_PHASE } = await loadStore();
    for (const [code, retryAfter] of [['budget_exhausted', null], ['ai_paused', 300]]) {
        const api = makeApi({ askInConversation: async () => { throw apiError(code, { retryAfter }); } });
        const store = createConversationStore({ api });
        assert.equal(await store.send('Q?'), false);
        const state = store.getState();
        assert.deepEqual(state.lastError, { code, message: code, retryAfter });
        assert.deepEqual(state.messages.map((m) => [m.content, m.status]), [['Q?', MESSAGE_STATUS.FAILED]]);
        assert.equal(state.sources.phase, SOURCES_PHASE.IDLE);
        assert.equal(state.sending, false);
    }
});

// ── undo delete ─────────────────────────────────────────────────────────

const LIST_FIXTURE = [
    { id: 'old', title: 'Old', updated_at: '2026-09-01T00:00:00Z' },
    { id: 'mid', title: 'Mid', updated_at: '2026-09-10T00:00:00Z' },
    { id: 'new', title: 'New', updated_at: '2026-09-20T00:00:00Z' },
];

test('remove() remembers the deleted header; undoRemove() restores it in sort order', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({ listConversations: async () => LIST_FIXTURE });
    const store = createConversationStore({ api });
    await store.loadList();

    assert.equal(await store.remove('mid'), true);
    let state = store.getState();
    assert.deepEqual(state.list.items.map((i) => i.id), ['new', 'old']);
    assert.equal(state.lastDeleted.id, 'mid');
    assert.equal(state.lastDeleted.header.title, 'Mid');
    assert.equal(state.lastDeleted.wasOpen, false);

    assert.equal(await store.undoRemove(), true);
    state = store.getState();
    assert.deepEqual(api.calls.find((c) => c[0] === 'restore').slice(1), ['mid']);
    assert.deepEqual(state.list.items.map((i) => i.id), ['new', 'mid', 'old'], 'back between newer and older');
    assert.equal(state.list.items[1].title, 'Restored', 'server header wins');
    assert.equal(state.lastDeleted, null);
    assert.equal(state.conversation, null, 'was not open, so nothing is opened');
    assert.equal(await store.undoRemove(), false, 'nothing left to undo');
});

test('undoRemove() reopens the thread that was open when it was deleted', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({
        getConversation: async (id) => ({ id, title: 'Mid', minhag: 'Ashkenaz', messages: [{ id: 'u1', role: 'user', content: 'q' }] }),
    });
    const store = createConversationStore({ api });
    await store.open('mid');
    await store.remove('mid');
    let state = store.getState();
    assert.equal(state.conversation, null);
    assert.equal(state.lastDeleted.wasOpen, true);
    assert.equal(state.lastDeleted.header.id, 'mid', 'header taken from the open thread when not in the list');

    assert.equal(await store.undoRemove(), true);
    state = store.getState();
    assert.equal(state.conversation.id, 'mid');
    assert.deepEqual(state.messages.map((m) => m.id), ['u1']);
    assert.deepEqual(state.list.items.map((i) => i.id), ['mid']);
});

test('undoRemove() does not yank the user out of a thread they moved to', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi();
    const store = createConversationStore({ api });
    await store.open('mid');
    await store.remove('mid');
    await store.open('other');
    assert.equal(await store.undoRemove(), true);
    assert.equal(store.getState().conversation.id, 'other');
    assert.equal(api.calls.filter((c) => c[0] === 'get').length, 2, 'no reopen of the restored thread');
});

test('a second delete replaces lastDeleted; only the most recent one undoes', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({ listConversations: async () => LIST_FIXTURE });
    const store = createConversationStore({ api });
    await store.loadList();
    await store.remove('old');
    await store.remove('new');
    assert.equal(store.getState().lastDeleted.id, 'new');
    await store.undoRemove();
    assert.deepEqual(api.calls.filter((c) => c[0] === 'restore').map((c) => c[1]), ['new']);
    assert.deepEqual(store.getState().list.items.map((i) => i.id), ['new', 'mid'], '"old" stays deleted');
});

test('failed delete leaves lastDeleted untouched', async () => {
    const { createConversationStore } = await loadStore();
    const api = makeApi({ listConversations: async () => LIST_FIXTURE });
    const store = createConversationStore({ api });
    await store.loadList();
    await store.remove('old');
    api.deleteConversation = async () => { throw apiError('network'); };
    assert.equal(await store.remove('new'), false);
    assert.equal(store.getState().lastDeleted.id, 'old');
    assert.equal(store.getState().lastError.code, 'network');
});

test('undoRemove() failure sets lastError, returns false, and keeps the undo available', async () => {
    const { createConversationStore } = await loadStore();
    let fail = true;
    const api = makeApi({
        listConversations: async () => LIST_FIXTURE,
        restoreConversation: async (id) => {
            if (fail) throw apiError('not_found');
            return null; // falls back to the remembered header
        },
    });
    const store = createConversationStore({ api });
    await store.loadList();
    await store.remove('mid');

    assert.equal(await store.undoRemove(), false);
    assert.equal(store.getState().lastError.code, 'not_found');
    assert.equal(store.getState().lastDeleted.id, 'mid');
    assert.ok(!store.getState().list.items.some((i) => i.id === 'mid'));

    fail = false;
    assert.equal(await store.undoRemove(), true);
    const items = store.getState().list.items;
    assert.deepEqual(items.map((i) => i.id), ['new', 'mid', 'old']);
    assert.equal(items[1].title, 'Mid', 'remembered header used when the restore body is empty');
});

test('a delete made while an undo is in flight is not cleared by that undo', async () => {
    const { createConversationStore } = await loadStore();
    const gate = deferred();
    const api = makeApi({
        listConversations: async () => LIST_FIXTURE,
        restoreConversation: async (id) => { await gate.promise; return { id }; },
    });
    const store = createConversationStore({ api });
    await store.loadList();
    await store.remove('mid');
    const undoing = store.undoRemove();
    await new Promise((r) => setImmediate(r));
    await store.remove('old');
    gate.resolve();
    assert.equal(await undoing, true);
    assert.equal(store.getState().lastDeleted.id, 'old');
});
