/**
 * static/js/ask-progress.js -- the live status of an AI answer: the step
 * state machine behind the status line, its bilingual labels, and the NDJSON
 * reader both ask clients share.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/ask-progress.js', {});
    return mod.namespace;
}

const start = (stage) => ({ type: 'stage', stage, state: 'start' });
const done = (stage) => ({ type: 'stage', stage, state: 'done' });

function apply(ns, events) {
    return events.reduce((state, event) => ns.applyProgressEvent(state, event), ns.createProgress());
}

// A Response whose body arrives in the given chunks (strings or bytes).
function streamResponse(chunks, { neverEnds = false } = {}) {
    const encoder = new TextEncoder();
    let i = 0;
    let cancelled = false;
    let release = null;
    return {
        cancelled: () => cancelled,
        body: {
            getReader: () => ({
                read: () => {
                    if (i < chunks.length) {
                        const chunk = chunks[i++];
                        return Promise.resolve({ done: false, value: typeof chunk === 'string' ? encoder.encode(chunk) : chunk });
                    }
                    if (!neverEnds) return Promise.resolve({ done: true, value: undefined });
                    return new Promise((resolve) => { release = () => resolve({ done: true, value: undefined }); });
                },
                cancel: async () => { cancelled = true; if (release) release(); },
            }),
        },
    };
}

test('events build up an ordered state, ignoring duplicates and unknown steps', async () => {
    const ns = await load();
    const state = apply(ns, [start('sources'), start('sources'), start('bogus'), { type: 'ping' }, done('sources')]);
    assert.deepEqual(state.order, ['sources']);
    assert.equal(state.status.sources, 'done');
    const unchanged = ns.applyProgressEvent(state, done('sources'));
    assert.equal(unchanged, state, 'an event that changes nothing returns the same object');
});

test('a step can run again after it finished', async () => {
    const ns = await load();
    const state = apply(ns, [start('commentary'), done('commentary'), start('commentary')]);
    assert.equal(state.status.commentary, 'active');
    assert.deepEqual(state.order, ['commentary']);
});

test('before anything starts there is nothing to name', async () => {
    const ns = await load();
    assert.deepEqual(ns.progressView(ns.createProgress()), { active: null, done: [] });
    assert.equal(ns.activeLabel(null, 'en'), 'Getting started');
    assert.equal(ns.activeLabel(null, 'he'), 'מתחילים');
});

test('the status names the longest-running step and lists finished ones in start order', async () => {
    const ns = await load();
    const state = apply(ns, [
        start('sources'), start('commentary'), start('customs'), start('times'),
        done('times'), done('customs'),
    ]);
    const view = ns.progressView(state);
    assert.equal(view.active, 'sources');
    assert.deepEqual(view.done, ['customs', 'times']);
});

test('when the running step ends the next one takes over', async () => {
    const ns = await load();
    const state = apply(ns, [
        start('sources'), start('commentary'), done('sources'),
    ]);
    assert.equal(ns.progressView(state).active, 'commentary');
});

test('the model call outranks lookups still winding down', async () => {
    const ns = await load();
    const state = apply(ns, [start('sources'), start('commentary'), done('sources'), start('thinking')]);
    assert.equal(ns.progressView(state).active, 'thinking');
});

test('once every lookup is done and the model has not started, the model is what remains', async () => {
    const ns = await load();
    const state = apply(ns, [start('sources'), done('sources'), start('times'), done('times')]);
    assert.equal(ns.progressView(state).active, 'thinking');
    assert.deepEqual(ns.progressView(state).done, ['sources', 'times']);
});

test('the thinking step is never listed as finished', async () => {
    const ns = await load();
    const state = apply(ns, [start('thinking'), done('thinking')]);
    assert.deepEqual(ns.progressView(state).done, []);
});

test('every step has an English and a Hebrew label', async () => {
    const ns = await load();
    for (const id of ns.STAGE_IDS) {
        assert.ok(ns.activeLabel(id, 'en').length > 3, `${id} en`);
        assert.match(ns.activeLabel(id, 'he'), /[֐-׿]/, `${id} he`);
        assert.notEqual(ns.activeLabel(id, 'en'), ns.activeLabel(id, 'he'));
        if (id !== 'thinking') {
            assert.ok(ns.doneLabel(id, 'en'));
            assert.match(ns.doneLabel(id, 'he'), /[֐-׿]/);
        }
    }
    assert.equal(ns.doneLabel('thinking', 'en'), '');
});

test('the stream reader reports steps and resolves with the terminal result', async () => {
    const ns = await load();
    const seen = [];
    const response = streamResponse([
        `${JSON.stringify(start('sources'))}\n${JSON.stringify({ type: 'ping' })}\n`,
        `${JSON.stringify({ type: 'result', payload: { answer: 'A' } })}\n`,
    ]);
    const terminal = await ns.readProgressStream(response, (event) => seen.push(event));
    assert.deepEqual(terminal, { type: 'result', payload: { answer: 'A' } });
    assert.deepEqual(seen, [start('sources'), { type: 'ping' }]);
    assert.equal(response.cancelled(), true, 'the body is released once the answer is in');
});

test('a line split across chunks, even inside a multi-byte Hebrew letter, is reassembled', async () => {
    const ns = await load();
    const bytes = new TextEncoder().encode(`${JSON.stringify({ type: 'result', payload: { answer: 'שלום' } })}\n`);
    // Each Hebrew letter is two bytes: cut between the halves of one.
    const cut = bytes.indexOf(0xd7) + 1;
    const response = streamResponse([bytes.slice(0, cut), bytes.slice(cut)]);
    const terminal = await ns.readProgressStream(response, () => {});
    assert.equal(terminal.payload.answer, 'שלום');
});

test('an unterminated final line is still read', async () => {
    const ns = await load();
    const response = streamResponse([JSON.stringify({ type: 'error', status: 500, detail: 'x' })]);
    const terminal = await ns.readProgressStream(response, () => {});
    assert.equal(terminal.type, 'error');
});

test('malformed lines are skipped, not fatal', async () => {
    const ns = await load();
    const response = streamResponse([
        `not json\n${JSON.stringify(start('times'))}\n{"type":\n${JSON.stringify({ type: 'result', payload: {} })}\n`,
    ]);
    const seen = [];
    const terminal = await ns.readProgressStream(response, (event) => seen.push(event));
    assert.equal(terminal.type, 'result');
    assert.deepEqual(seen, [start('times')]);
});

test('a throwing progress handler never aborts the answer', async () => {
    const ns = await load();
    const response = streamResponse([
        `${JSON.stringify(start('sources'))}\n${JSON.stringify({ type: 'result', payload: { ok: 1 } })}\n`,
    ]);
    const terminal = await ns.readProgressStream(response, () => { throw new Error('ui blew up'); });
    assert.deepEqual(terminal.payload, { ok: 1 });
});

test('a stream that ends without a terminal line rejects as interrupted', async () => {
    const ns = await load();
    const response = streamResponse([`${JSON.stringify(start('sources'))}\n`]);
    await assert.rejects(ns.readProgressStream(response, () => {}), (error) => {
        assert.equal(error.interrupted, true);
        assert.match(error.message, /interrupted/);
        return true;
    });
});

test('a stream that outlives its deadline is cut and rejects', async () => {
    const ns = await load();
    const response = streamResponse([`${JSON.stringify(start('sources'))}\n`], { neverEnds: true });
    await assert.rejects(ns.readProgressStream(response, () => {}, { timeoutMs: 20 }), (error) => {
        assert.equal(error.interrupted, true);
        assert.match(error.message, /too long/);
        return true;
    });
    assert.equal(response.cancelled(), true);
});

test('without a streaming body the whole text is read as NDJSON', async () => {
    const ns = await load();
    const text = `${JSON.stringify(start('sources'))}\n${JSON.stringify({ type: 'result', payload: { answer: 'B' } })}\n`;
    const seen = [];
    const terminal = await ns.readProgressStream({ text: async () => text }, (event) => seen.push(event));
    assert.equal(terminal.payload.answer, 'B');
    assert.deepEqual(seen, [start('sources')]);
});

test('only an ndjson content type is treated as a progress stream', async () => {
    const ns = await load();
    const withType = (value) => ({ headers: { get: () => value } });
    assert.equal(ns.isProgressResponse(withType('application/x-ndjson')), true);
    assert.equal(ns.isProgressResponse(withType('Application/X-NDJSON; charset=utf-8')), true);
    assert.equal(ns.isProgressResponse(withType('application/json')), false);
    assert.equal(ns.isProgressResponse({}), false);
});
