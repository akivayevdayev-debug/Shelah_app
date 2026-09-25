/**
 * static/js/conversation-api.js -- the /api/conversations/* client.
 *
 * The behaviors worth pinning are the ones that keep a flaky network from
 * corrupting a thread: POST .../ask saves the user's turn before the model
 * call, so a transient failure must reconcile against the stored thread
 * instead of blindly re-sending (which would duplicate the question); create
 * is never retried; idempotent calls retry on 502/503/504/network; a 401
 * refreshes auth once; and every failure surfaces a stable `.code`.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const API_PATH = 'static/js/conversation-api.js';

function jsonResponse(status, body, headers = {}) {
    return {
        status,
        ok: status >= 200 && status < 300,
        headers: { get: (name) => headers[name] ?? null },
        json: async () => body,
    };
}

function networkError() {
    return new TypeError('Failed to fetch');
}

// Routes each call to the first handler whose [method, pathRegex] matches;
// a handler is an array consumed in order (holding on its last entry).
function makeRoutedFetch(routes) {
    const calls = [];
    const cursors = new Map();
    const fetchFn = async (url, opts = {}) => {
        const method = opts.method || 'GET';
        calls.push({ url, method, body: opts.body ? JSON.parse(opts.body) : undefined, headers: opts.headers });
        const route = routes.find(([m, re]) => m === method && re.test(url));
        if (!route) throw new Error(`unexpected fetch ${method} ${url}`);
        const seq = route[2];
        const i = cursors.get(route) || 0;
        cursors.set(route, i + 1);
        const entry = seq[Math.min(i, seq.length - 1)];
        if (entry instanceof Error) throw entry;
        return typeof entry === 'function' ? entry() : entry;
    };
    fetchFn.calls = calls;
    return fetchFn;
}

async function loadApi(fetchFn, { authHeaders } = {}) {
    const window = { authHeaders: authHeaders || (async (base) => ({ ...base, Authorization: 'Bearer t' })) };
    const mod = await loadEsmModule(API_PATH, {
        window,
        fetch: fetchFn,
        // Collapse backoff and abort watchdogs so tests run instantly.
        setTimeout: (fn, ms) => (ms >= 60000 ? 0 : (fn(), 0)),
        clearTimeout: () => {},
        AbortController,
    });
    return mod.namespace;
}

const ASK_RE = /\/api\/conversations\/c1\/ask$/;
const GET_RE = /\/api\/conversations\/c1$/;
const USER_MSG = { id: 'u1', role: 'user', content: 'Q?', status: 'complete', citations: [] };
const ANSWER = { id: 'a1', role: 'assistant', content: 'A.', status: 'complete', citations: [{ ordinal: 0, source_ref: 'SA OC 1:1' }] };
const ASK_OK = { user_message: USER_MSG, assistant_message: ANSWER, conversation: { id: 'c1', title: 'Q?' } };

test('askInConversation happy path posts question/mode/language with auth and returns the 201 body', async () => {
    const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(201, ASK_OK)]]]);
    const api = await loadApi(fetchFn);

    const result = await api.askInConversation('c1', { question: '  Q?  ', mode: 'balanced', language: 'en' });

    assert.deepEqual(result, ASK_OK);
    assert.equal(fetchFn.calls.length, 1);
    assert.deepEqual(fetchFn.calls[0].body, { question: 'Q?', mode: 'balanced', language: 'en' });
    assert.equal(fetchFn.calls[0].headers.Authorization, 'Bearer t');
    assert.equal(fetchFn.calls[0].headers['Content-Type'], 'application/json');
});

test('askInConversation: a stored error turn (201, status=error) resolves, it does not throw', async () => {
    const body = { ...ASK_OK, assistant_message: { role: 'assistant', content: '', status: 'error', citations: [] } };
    const api = await loadApi(makeRoutedFetch([['POST', ASK_RE, [jsonResponse(201, body)]]]));
    const result = await api.askInConversation('c1', { question: 'Q?' });
    assert.equal(result.assistant_message.status, 'error');
});

test('askInConversation: transient failure where the question never landed re-sends once and succeeds', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(504, {}), jsonResponse(201, ASK_OK)]],
        ['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages: [] })]],
    ]);
    const retries = [];
    const api = await loadApi(fetchFn);

    const result = await api.askInConversation('c1', { question: 'Q?' }, { onRetry: (n) => retries.push(n) });

    assert.deepEqual(result, ASK_OK);
    assert.deepEqual(fetchFn.calls.map((c) => c.method), ['POST', 'GET', 'POST']);
    assert.deepEqual(retries, [2]);
});

test('askInConversation: timeout after the server saved AND answered recovers the stored turns without re-sending', async () => {
    const thread = { id: 'c1', title: 'Q?', messages: [{ id: 'old', role: 'user', content: 'Q?' }, USER_MSG, ANSWER] };
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [networkError()]],
        ['GET', GET_RE, [jsonResponse(200, thread)]],
    ]);
    const api = await loadApi(fetchFn);

    // 'old' is an earlier identical question -- known ids exclude it.
    const result = await api.askInConversation('c1', { question: 'Q?' }, { knownMessageIds: ['old'] });

    assert.equal(result.user_message.id, 'u1');
    assert.equal(result.assistant_message.id, 'a1');
    assert.equal(fetchFn.calls.filter((c) => c.method === 'POST').length, 1, 'must not re-send a saved question');
});

test('askInConversation: question saved but no answer yet throws answer_incomplete carrying the saved turn', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [networkError()]],
        ['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages: [USER_MSG] })]],
    ]);
    const api = await loadApi(fetchFn);

    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), (err) => {
        assert.equal(err.code, api.ERROR_CODES.ANSWER_INCOMPLETE);
        assert.equal(err.userMessage.id, 'u1');
        return true;
    });
    assert.equal(fetchFn.calls.filter((c) => c.method === 'POST').length, 1);
});

test('askInConversation: if the reconcile read itself fails, it surfaces the error instead of guessing', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(503, { error: 'x' })]],
        ['GET', GET_RE, [jsonResponse(500, {})]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), { code: 'unavailable', status: 503 });
    assert.equal(fetchFn.calls.filter((c) => c.method === 'POST').length, 1);
});

test('askInConversation: 4xx is not retried and maps to a stable code', async () => {
    for (const [status, code] of [[400, 'invalid'], [404, 'not_found'], [403, 'forbidden']]) {
        const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(status, { error: 'nope' })]]]);
        const api = await loadApi(fetchFn);
        await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), { code, status, message: 'nope' });
        assert.equal(fetchFn.calls.length, 1);
    }
});

test('askInConversation: 429 is not retried and exposes Retry-After', async () => {
    const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(429, { code: 'rate_limited' }, { 'Retry-After': '60' })]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), { code: 'rate_limited', retryAfter: 60 });
    assert.equal(fetchFn.calls.length, 1);
});

test('askInConversation: gives up with a network error after three never-landed attempts', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [networkError()]],
        ['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages: [] })]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), { code: 'network' });
    assert.equal(fetchFn.calls.filter((c) => c.method === 'POST').length, 3);
});

test('a 401 refreshes auth once and repeats the call', async () => {
    let tokenCalls = 0;
    const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(401, {}), jsonResponse(201, ASK_OK)]]]);
    const api = await loadApi(fetchFn, {
        authHeaders: async (base) => ({ ...base, Authorization: `Bearer t${++tokenCalls}` }),
    });

    await api.askInConversation('c1', { question: 'Q?' });

    assert.deepEqual(fetchFn.calls.map((c) => c.headers.Authorization), ['Bearer t1', 'Bearer t2']);
});

test('a second 401 surfaces as unauthorized', async () => {
    const fetchFn = makeRoutedFetch([['GET', /\/api\/conversations\?/, [jsonResponse(401, {})]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.listConversations(), { code: 'unauthorized' });
    assert.equal(fetchFn.calls.length, 2);
});

test('createConversation is never retried (a lost response would orphan an extra thread)', async () => {
    const fetchFn = makeRoutedFetch([['POST', /\/api\/conversations$/, [jsonResponse(502, {})]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.createConversation({ minhag: 'Sefardic' }), { code: 'server', status: 502 });
    assert.equal(fetchFn.calls.length, 1);
    assert.deepEqual(fetchFn.calls[0].body, { minhag: 'Sefardic' });
});

test('createConversation sends null for a blank minhag', async () => {
    const fetchFn = makeRoutedFetch([['POST', /\/api\/conversations$/, [jsonResponse(201, { id: 'c1' })]]]);
    const api = await loadApi(fetchFn);
    await api.createConversation({});
    assert.deepEqual(fetchFn.calls[0].body, { minhag: null });
});

test('idempotent reads retry on 503 then succeed', async () => {
    const fetchFn = makeRoutedFetch([['GET', /\/api\/conversations\?limit=5$/, [jsonResponse(503, {}), jsonResponse(200, { items: [{ id: 'c1' }] })]]]);
    const api = await loadApi(fetchFn);
    assert.deepEqual(await api.listConversations({ limit: 5 }), [{ id: 'c1' }]);
    assert.equal(fetchFn.calls.length, 2);
});

test('rename / pin / delete hit the right method, path and body; ids are URL-encoded', async () => {
    const fetchFn = makeRoutedFetch([
        ['PATCH', /\/api\/conversations\/a%2Fb$/, [jsonResponse(200, { id: 'a/b' })]],
        ['DELETE', /\/api\/conversations\/a%2Fb$/, [jsonResponse(200, { ok: true })]],
    ]);
    const api = await loadApi(fetchFn);
    await api.renameConversation('a/b', 'New title');
    await api.setConversationPinned('a/b', 1);
    await api.deleteConversation('a/b');
    assert.deepEqual(fetchFn.calls.map((c) => [c.method, c.body]), [
        ['PATCH', { title: 'New title' }],
        ['PATCH', { pinned: true }],
        ['DELETE', undefined],
    ]);
});

test('works signed-out (no window.authHeaders): sends no Authorization header', async () => {
    const fetchFn = makeRoutedFetch([['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages: [] })]]]);
    const mod = await loadEsmModule(API_PATH, {
        window: {}, fetch: fetchFn, setTimeout: () => 0, clearTimeout: () => {}, AbortController,
    });
    await mod.namespace.getConversation('c1');
    assert.equal(fetchFn.calls[0].headers.Authorization, undefined);
});

// ── cost guards ─────────────────────────────────────────────────────────

test('askInConversation: 402 daily_budget_exhausted is not retried or reconciled', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(402, { error: 'Daily limit reached', code: 'daily_budget_exhausted' })]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), {
        code: api.ERROR_CODES.BUDGET_EXHAUSTED, status: 402, message: 'Daily limit reached',
    });
    assert.deepEqual(fetchFn.calls.map((c) => c.method), ['POST'], 'no GET reconcile, no re-send');
});

test('a 402 maps to budget_exhausted even without a body code', async () => {
    const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(402, null)]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), { code: 'budget_exhausted', status: 402 });
});

test('askInConversation: 503 ai_paused is not transient and carries Retry-After', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(503, { error: 'Paused', code: 'ai_paused' }, { 'Retry-After': '300' })]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), {
        code: api.ERROR_CODES.AI_PAUSED, status: 503, retryAfter: 300,
    });
    assert.deepEqual(fetchFn.calls.map((c) => c.method), ['POST'], 'no GET reconcile, no re-send');
});

test('a 503 without the ai_paused code on another endpoint keeps the plain retry + unavailable code', async () => {
    const fetchFn = makeRoutedFetch([['GET', GET_RE, [jsonResponse(503, { error: 'down' })]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.getConversation('c1'), { code: 'unavailable', status: 503 });
    assert.equal(fetchFn.calls.length, 3);
});

// ── restore ─────────────────────────────────────────────────────────────

test('restoreConversation POSTs .../restore with no body and returns the header', async () => {
    const fetchFn = makeRoutedFetch([['POST', /\/api\/conversations\/a%2Fb\/restore$/, [jsonResponse(200, { id: 'a/b', title: 'T' })]]]);
    const api = await loadApi(fetchFn);
    assert.deepEqual(await api.restoreConversation('a/b'), { id: 'a/b', title: 'T' });
    assert.deepEqual(fetchFn.calls.map((c) => [c.method, c.body]), [['POST', undefined]]);
});

test('restoreConversation is idempotent, so it retries a transient failure', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', /\/restore$/, [networkError(), jsonResponse(502, {}), jsonResponse(200, { id: 'c1' })]],
    ]);
    const api = await loadApi(fetchFn);
    assert.deepEqual(await api.restoreConversation('c1'), { id: 'c1' });
    assert.equal(fetchFn.calls.length, 3);
});

test('restoreConversation 404 maps to not_found', async () => {
    const fetchFn = makeRoutedFetch([['POST', /\/restore$/, [jsonResponse(404, { error: 'Not found' })]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.restoreConversation('c1'), { code: 'not_found', status: 404 });
});

// ── retry_of ────────────────────────────────────────────────────────────

const ERR_ROW = { id: 'e1', role: 'assistant', content: '', status: 'error' };
const RETRY_ANSWER = { id: 'a2', role: 'assistant', content: 'A2.', status: 'complete', citations: [] };
const RETRY_OK = { user_message: USER_MSG, assistant_message: RETRY_ANSWER, conversation: { id: 'c1' }, superseded_message_id: 'e1' };
const RETRY_KNOWN = ['u1', 'e1'];

test('askInConversation sends retry_of and returns the 201 retry body', async () => {
    const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(201, RETRY_OK)]]]);
    const api = await loadApi(fetchFn);
    const result = await api.askInConversation('c1', { question: 'Q?', mode: 'balanced', language: 'en', retryOf: 'e1' });
    assert.deepEqual(result, RETRY_OK);
    assert.deepEqual(fetchFn.calls[0].body, { question: 'Q?', mode: 'balanced', language: 'en', retry_of: 'e1' });
});

test('retry: transient failure after the retry landed returns the new answer without re-sending', async () => {
    // The error row is gone (superseded); the fresh reply follows the known question.
    const thread = { id: 'c1', messages: [USER_MSG, RETRY_ANSWER] };
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [networkError()]],
        ['GET', GET_RE, [jsonResponse(200, thread)]],
    ]);
    const api = await loadApi(fetchFn);
    const result = await api.askInConversation('c1', { question: 'Q?', retryOf: 'e1' }, { knownMessageIds: RETRY_KNOWN });
    assert.equal(result.user_message.id, 'u1');
    assert.equal(result.assistant_message.id, 'a2');
    assert.equal(result.conversation, thread);
    assert.deepEqual(fetchFn.calls.map((c) => c.method), ['POST', 'GET']);
});

test('retry: transient failure that never landed re-sends the same retry_of', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(504, {}), jsonResponse(201, RETRY_OK)]],
        ['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages: [USER_MSG, ERR_ROW] })]],
    ]);
    const api = await loadApi(fetchFn);
    const result = await api.askInConversation('c1', { question: 'Q?', retryOf: 'e1' }, { knownMessageIds: RETRY_KNOWN });
    assert.deepEqual(result, RETRY_OK);
    const posts = fetchFn.calls.filter((c) => c.method === 'POST');
    assert.equal(posts.length, 2);
    assert.equal(posts[1].body.retry_of, 'e1');
});

test('retry: re-send answered invalid_retry (first attempt landed) re-reads and returns that answer', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [networkError(), jsonResponse(400, { error: 'bad retry', code: 'invalid_retry' })]],
        ['GET', GET_RE, [
            // First read: not visible yet (still the error row) -> safe to re-send.
            jsonResponse(200, { id: 'c1', messages: [USER_MSG, ERR_ROW] }),
            // Second read, after invalid_retry: the first attempt's answer.
            jsonResponse(200, { id: 'c1', messages: [USER_MSG, RETRY_ANSWER] }),
        ]],
    ]);
    const api = await loadApi(fetchFn);
    const result = await api.askInConversation('c1', { question: 'Q?', retryOf: 'e1' }, { knownMessageIds: RETRY_KNOWN });
    assert.equal(result.assistant_message.id, 'a2');
    assert.deepEqual(fetchFn.calls.map((c) => c.method), ['POST', 'GET', 'POST', 'GET']);
});

test('retry: invalid_retry with no fresh answer in the thread throws invalid_retry', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(400, { error: 'bad retry', code: 'invalid_retry' })]],
        ['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages: [USER_MSG, ERR_ROW] })]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(
        api.askInConversation('c1', { question: 'Q?', retryOf: 'e1' }, { knownMessageIds: RETRY_KNOWN }),
        { code: api.ERROR_CODES.INVALID_RETRY, status: 400 },
    );
    assert.equal(fetchFn.calls.filter((c) => c.method === 'POST').length, 1, 'never re-sends after invalid_retry');
});

test('retry: invalid_retry whose re-read fails surfaces invalid_retry', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(400, { code: 'invalid_retry' })]],
        ['GET', GET_RE, [jsonResponse(404, {})]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?', retryOf: 'e1' }, { knownMessageIds: RETRY_KNOWN }), { code: 'invalid_retry' });
});

test('invalid_retry without retryOf is just a 400 (no re-read)', async () => {
    const fetchFn = makeRoutedFetch([['POST', ASK_RE, [jsonResponse(400, { code: 'invalid_retry' })]]]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?' }), { code: 'invalid_retry' });
    assert.equal(fetchFn.calls.length, 1);
});

test('retry: if the reconcile read fails after a transient failure, it surfaces the error instead of re-sending', async () => {
    const fetchFn = makeRoutedFetch([
        ['POST', ASK_RE, [jsonResponse(502, {})]],
        ['GET', GET_RE, [jsonResponse(500, {})]],
    ]);
    const api = await loadApi(fetchFn);
    await assert.rejects(api.askInConversation('c1', { question: 'Q?', retryOf: 'e1' }, { knownMessageIds: RETRY_KNOWN }), { code: 'server', status: 502 });
    assert.equal(fetchFn.calls.filter((c) => c.method === 'POST').length, 1);
});

test('reconcileRetry ignores replies to other questions and replies to brand-new questions', async () => {
    const other = { id: 'u0', role: 'user', content: 'Earlier?' };
    const newQ = { id: 'u5', role: 'user', content: 'From another tab' };
    const cases = [
        // Error row still visible: a new reply under a DIFFERENT known question doesn't count.
        [[other, { id: 'x1', role: 'assistant' }, USER_MSG, ERR_ROW], 'not_saved'],
        // A new reply under a new question (another tab's send) doesn't count either.
        [[USER_MSG, newQ, { id: 'x2', role: 'assistant' }], 'not_saved'],
        // No preceding user message at all.
        [[{ id: 'x3', role: 'assistant' }], 'not_saved'],
        // Error row still visible but the fresh reply already follows its question.
        [[USER_MSG, RETRY_ANSWER, ERR_ROW], 'answered'],
    ];
    for (const [messages, outcome] of cases) {
        const fetchFn = makeRoutedFetch([['GET', GET_RE, [jsonResponse(200, { id: 'c1', messages })]]]);
        const api = await loadApi(fetchFn);
        const state = await api.reconcileRetry('c1', 'e1', ['u0', 'u1', 'e1']);
        assert.equal(state.outcome, outcome, JSON.stringify(messages.map((m) => m.id)));
    }
});

test('reconcileRetry tolerates a thread with no messages array', async () => {
    const fetchFn = makeRoutedFetch([['GET', GET_RE, [jsonResponse(200, { id: 'c1' })]]]);
    const api = await loadApi(fetchFn);
    assert.deepEqual(await api.reconcileRetry('c1', 'e1'), { outcome: 'not_saved' });
});
