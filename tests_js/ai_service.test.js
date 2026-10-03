/**
 * The /ask reconciliation. static/js/ai-service.js::askAi() is the sole
 * implementation of POST /ask; it absorbed the retry/timeout resilience that
 * used to live only in templates/index.html's inline askWithRetry(). These
 * tests are the "fails when the extraction is reverted" floor:
 * they cover the exact behaviors that made the inline copy the stronger
 * implementation (60s per-attempt timeout, 3-attempt retry with backoff on
 * 502/503/504, retry on AbortError/TypeError, no retry on a clean 4xx,
 * attempt-count telemetry on network-exhaustion failures) plus the module's
 * own return contract (parsed payload on success, a thrown Error carrying
 * `.status`/`.code`/`.attempts` on failure).
 *
 * Run with: node --test tests_js/*.test.js
 * (wired into `npm test`; see tests_js/helpers/esm_harness.js for why real
 * static/js/*.js ES modules need a module-hook-based loader in Node.)
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const AI_SERVICE_PATH = 'static/js/ai-service.js';

function makeJsonResponse(status, body) {
    return {
        status,
        ok: status >= 200 && status < 300,
        json: async () => body,
    };
}

function makeAbortError() {
    const err = new Error('The operation was aborted');
    err.name = 'AbortError';
    return err;
}

// Returns queued responses/errors in order, holding on the last entry once
// exhausted (never needed here since every test supplies exactly as many
// entries as expected fetch calls, but guards against an off-by-one hang).
function makeSequenceFetch(sequence) {
    const calls = [];
    let i = 0;
    const fetchFn = async (url, opts) => {
        calls.push({ url, opts });
        const entry = sequence[Math.min(i, sequence.length - 1)];
        i += 1;
        if (entry instanceof Error) {
            throw entry;
        }
        return entry;
    };
    fetchFn.calls = calls;
    return fetchFn;
}

async function loadAiService(globalsOverrides = {}) {
    const window = { ...globalsOverrides.window };
    const mod = await loadEsmModule(AI_SERVICE_PATH, {
        window,
        fetch: globalsOverrides.fetch,
        // Fire every timer synchronously and immediately: this collapses the
        // 1200ms-x-attempt backoff to nothing (tests must not take 6+ real
        // seconds) and also fires the 60s abort watchdog immediately, but that
        // is harmless here because none of these fetch mocks inspect
        // `opts.signal` -- calling `abortCtrl.abort()` on an ignored signal has
        // no observable effect on a mock's own resolution/rejection.
        setTimeout: globalsOverrides.setTimeout || ((fn) => {
            fn();
            return 0;
        }),
        clearTimeout: () => {},
        AbortController,
    });
    return { mod, window };
}

// Fires the short backoff timers at once but leaves the 60s watchdogs (the
// abort timer and the stream reader's deadline) unfired.
const setTimeoutSparingWatchdogs = (fn, ms) => (ms >= 60000 ? 0 : (fn(), 0));

function makeNdjsonResponse(events, status = 200) {
    const encoder = new TextEncoder();
    const chunks = events.map((event) => encoder.encode(`${JSON.stringify(event)}\n`));
    let i = 0;
    return {
        status,
        ok: status >= 200 && status < 300,
        headers: { get: (name) => (String(name).toLowerCase() === 'content-type' ? 'application/x-ndjson' : null) },
        body: {
            getReader: () => ({
                read: async () => (i < chunks.length ? { done: false, value: chunks[i++] } : { done: true }),
                cancel: async () => {},
            }),
        },
    };
}

test('askAi happy path: single attempt, resolves with the parsed payload, updates state.ai, no retries', async () => {
    const payload = { answer: 'test answer', sources: [] };
    const fetchFn = makeSequenceFetch([makeJsonResponse(200, payload)]);
    const onRetryCalls = [];
    const { mod, window } = await loadAiService({ fetch: fetchFn });

    const result = await mod.namespace.askAi('What is X?', {
        onRetry: (attempt) => onRetryCalls.push(attempt),
    });

    assert.deepEqual(result, payload);
    assert.equal(fetchFn.calls.length, 1, 'a clean 200 must not be retried');
    assert.deepEqual(onRetryCalls, []);
    assert.equal(window.appState.ai.pending, false);
    assert.equal(window.appState.ai.lastError, null);
    assert.deepEqual(window.appState.ai.lastResponse, payload);
});

test('askAi retries once on a 502 then succeeds, reporting the retry via onRetry', async () => {
    const payload = { answer: 'ok after retry' };
    const fetchFn = makeSequenceFetch([
        makeJsonResponse(502, { error: 'bad gateway' }),
        makeJsonResponse(200, payload),
    ]);
    const onRetryCalls = [];
    const { mod } = await loadAiService({ fetch: fetchFn });

    const result = await mod.namespace.askAi('question', {
        onRetry: (attempt) => onRetryCalls.push(attempt),
    });

    assert.deepEqual(result, payload);
    assert.equal(fetchFn.calls.length, 2);
    assert.deepEqual(onRetryCalls, [2], 'onRetry fires once, for the 2nd attempt');
});

test('askAi exhausts all 3 attempts against a persistent 503 and throws with the final HTTP status, no attempt telemetry', async () => {
    const fetchFn = makeSequenceFetch([
        makeJsonResponse(503, { error: 'unavailable' }),
        makeJsonResponse(503, { error: 'unavailable' }),
        makeJsonResponse(503, { error: 'unavailable' }),
    ]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    await assert.rejects(mod.namespace.askAi('question', {}), (error) => {
        assert.equal(error.status, 503);
        assert.equal(error.message, 'unavailable');
        assert.equal(
            error.attempts,
            undefined,
            'a completed-but-unhealthy final response is not a network-exhaustion failure -- .attempts stays unset',
        );
        return true;
    });
    assert.equal(fetchFn.calls.length, 3, 'a persistently-retryable status must still be attempted exactly AI_MAX_ATTEMPTS times');
});

test('askAi retries on AbortError (timeout) and throws with attempt telemetry after exhausting retries', async () => {
    const fetchFn = makeSequenceFetch([makeAbortError(), makeAbortError(), makeAbortError()]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    await assert.rejects(mod.namespace.askAi('question', {}), (error) => {
        assert.equal(error.name, 'AbortError');
        assert.equal(error.attempts, 3, 'network-exception exhaustion must carry attempt-count telemetry');
        return true;
    });
    assert.equal(fetchFn.calls.length, 3);
});

test('askAi does not retry a clean 4xx response', async () => {
    const fetchFn = makeSequenceFetch([makeJsonResponse(400, { error: 'bad input' })]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    await assert.rejects(mod.namespace.askAi('question', {}), (error) => {
        assert.equal(error.status, 400);
        assert.equal(error.message, 'bad input');
        return true;
    });
    assert.equal(fetchFn.calls.length, 1, 'a 4xx must fail fast, never retried');
});

test('askAi retries a network TypeError once and reports the retry via onRetry', async () => {
    const fetchFn = makeSequenceFetch([new TypeError('Failed to fetch'), makeJsonResponse(200, { answer: 'ok' })]);
    const { mod } = await loadAiService({ fetch: fetchFn });
    const retryAttempts = [];

    const payload = await mod.namespace.askAi('question', { onRetry: (attempt) => retryAttempts.push(attempt) });

    assert.deepEqual(payload, { answer: 'ok' });
    assert.equal(fetchFn.calls.length, 2);
    assert.deepEqual(retryAttempts, [2]);
});

test('askAi does not let a throwing onRetry hook abort the request it is reporting on', async () => {
    const fetchFn = makeSequenceFetch([makeJsonResponse(503, {}), makeJsonResponse(200, { answer: 'ok' })]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    const payload = await mod.namespace.askAi('question', {
        onRetry: () => {
            throw new Error('retry UI blew up');
        },
    });

    assert.deepEqual(payload, { answer: 'ok' });
    assert.equal(fetchFn.calls.length, 2);
});

test('askAi fails fast on an error that is neither an abort nor a network TypeError', async () => {
    const fetchFn = makeSequenceFetch([new RangeError('not a transient failure')]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    await assert.rejects(mod.namespace.askAi('question', {}), (error) => {
        assert.equal(error.name, 'RangeError');
        assert.equal(error.attempts, 1);
        return true;
    });
    assert.equal(fetchFn.calls.length, 1, 'only AbortError/TypeError are retried');
});

test('askAi asks for the progress stream and still accepts plain JSON', async () => {
    const fetchFn = makeSequenceFetch([makeJsonResponse(200, { answer: 'plain' })]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    const result = await mod.namespace.askAi('question', { onProgress: () => {} });

    assert.deepEqual(result, { answer: 'plain' });
    assert.match(fetchFn.calls[0].opts.headers.Accept, /application\/x-ndjson/);
    assert.match(fetchFn.calls[0].opts.headers.Accept, /application\/json/);
});

test('askAi forwards each pipeline step to onProgress, then resolves with the streamed answer', async () => {
    const payload = { answer: 'streamed', sources: [] };
    const fetchFn = makeSequenceFetch([makeNdjsonResponse([
        { type: 'stage', stage: 'sources', state: 'start' },
        { type: 'stage', stage: 'sources', state: 'done' },
        { type: 'stage', stage: 'thinking', state: 'start' },
        { type: 'result', payload },
    ])]);
    const { mod, window } = await loadAiService({ fetch: fetchFn, setTimeout: setTimeoutSparingWatchdogs });
    const steps = [];

    const result = await mod.namespace.askAi('question', { onProgress: (event) => steps.push(`${event.stage}:${event.state}`) });

    assert.deepEqual(result, payload);
    assert.deepEqual(steps, ['sources:start', 'sources:done', 'thinking:start']);
    assert.equal(fetchFn.calls.length, 1);
    assert.equal(window.appState.ai.pending, false);
    assert.deepEqual(window.appState.ai.lastResponse, payload);
});

test('askAi turns a failure reported inside the stream into the usual status/code error', async () => {
    const fetchFn = makeSequenceFetch([makeNdjsonResponse([
        { type: 'stage', stage: 'sources', state: 'start' },
        { type: 'error', status: 500, detail: { error: 'Something broke', code: 'internal' } },
    ])]);
    const { mod, window } = await loadAiService({ fetch: fetchFn, setTimeout: setTimeoutSparingWatchdogs });

    await assert.rejects(mod.namespace.askAi('question', {}), (error) => {
        assert.equal(error.status, 500);
        assert.equal(error.code, 'internal');
        assert.equal(error.message, 'Something broke');
        return true;
    });
    assert.equal(window.appState.ai.pending, false);
    assert.equal(window.appState.ai.lastError, 'Something broke');
});

test('askAi does not re-send a question whose stream was cut off', async () => {
    const fetchFn = makeSequenceFetch([makeNdjsonResponse([
        { type: 'stage', stage: 'sources', state: 'start' },
    ])]);
    const { mod, window } = await loadAiService({ fetch: fetchFn, setTimeout: setTimeoutSparingWatchdogs });

    await assert.rejects(mod.namespace.askAi('question', {}), (error) => {
        assert.equal(error.interrupted, true);
        return true;
    });
    assert.equal(fetchFn.calls.length, 1, 'an answer already being generated is never paid for twice');
    assert.equal(window.appState.ai.pending, false);
});

test('askAi passes a refusal that came before the stream straight through', async () => {
    const fetchFn = makeSequenceFetch([makeJsonResponse(402, { detail: 'Daily AI usage limit reached' })]);
    const { mod } = await loadAiService({ fetch: fetchFn });

    await assert.rejects(mod.namespace.askAi('question', { onProgress: () => {} }), (error) => {
        assert.equal(error.status, 402);
        assert.equal(error.message, 'Daily AI usage limit reached');
        return true;
    });
});
