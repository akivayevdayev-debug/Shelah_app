/**
 * static/js/conversation-share.js -- the header Share button's controller:
 * publish() mints (or re-uses) the conversation's public link and copies it,
 * with the clipboard write started inside the click; stop() revokes it.
 *
 * The share store behind it is answer-share.js's, built here from the real
 * defaults (conversation base path, no private fallback, refresh on share)
 * against a fake fetch that answers like backend/routes_conversation_share.py.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const ID = '3f2b8c1e-9a4d-4e6f-8b1a-2c3d4e5f6a7b';
const TOKEN = 't'.repeat(22);
const ORIGIN = 'https://shelah.org';

function jsonResponse(status, body) {
    return { status, ok: status >= 200 && status < 300, json: async () => body };
}

// A share is 201 and then 200; the state read is the live token; DELETE
// clears it. `fail` makes every request answer that status instead.
function fakeServer({ fail = null, failBody = {} } = {}) {
    const requests = [];
    let live = false;
    const fetchImpl = async (url, opts) => {
        requests.push([opts.method, url]);
        if (fail) return jsonResponse(fail, failBody);
        if (opts.method === 'POST') {
            const status = live ? 200 : 201;
            live = true;
            return jsonResponse(status, { shared: true, share_token: TOKEN });
        }
        if (opts.method === 'DELETE') {
            live = false;
            return jsonResponse(200, { shared: false });
        }
        return jsonResponse(200, live ? { shared: true, share_token: TOKEN } : { shared: false });
    };
    return { requests, fetchImpl };
}

async function setup({ server = fakeServer(), copyResult = true, copyThrows = false } = {}) {
    const mod = await loadEsmModule('static/js/conversation-share.js', {
        window: { location: { origin: ORIGIN } },
        fetch: server.fetchImpl,
        navigator: {},
        document: {},
    });
    const copied = [];
    const share = mod.namespace.createConversationShare({
        copy: async (link) => {
            if (copyThrows) throw new Error('clipboard');
            copied.push(await link);
            return copyResult;
        },
    });
    return { share, server, copied };
}

test('publish POSTs the conversation share and copies its public /a/<token> link', async () => {
    const { share, server, copied } = await setup();
    assert.deepEqual(await share.publish(ID), { status: 'copied', url: `${ORIGIN}/a/${TOKEN}` });
    assert.deepEqual(copied, [`${ORIGIN}/a/${TOKEN}`]);
    assert.deepEqual(server.requests, [['POST', `/api/conversations/${ID}/share`]]);
    assert.deepEqual(share.peek(ID), { shared: true, token: TOKEN });
});

test('publishing again posts again, so the snapshot moves to the newest turn', async () => {
    const { share, server } = await setup();
    await share.publish(ID);
    const second = await share.publish(ID);
    assert.equal(second.url, `${ORIGIN}/a/${TOKEN}`);
    assert.deepEqual(server.requests.map(([method]) => method), ['POST', 'POST']);
});

test('a refused clipboard still yields the link, for the caller to show', async () => {
    const refused = await setup({ copyResult: false });
    assert.deepEqual(await refused.share.publish(ID), { status: 'manual', url: `${ORIGIN}/a/${TOKEN}` });
    const threw = await setup({ copyThrows: true });
    assert.deepEqual(await threw.share.publish(ID), { status: 'manual', url: `${ORIGIN}/a/${TOKEN}` });
});

test('an empty conversation is "empty", any other failure is "failed"', async () => {
    const empty = await setup({ server: fakeServer({ fail: 409, failBody: { code: 'empty_conversation' } }) });
    assert.deepEqual(await empty.share.publish(ID), { status: 'empty' });
    assert.deepEqual(empty.copied, [], 'nothing was copied');

    const down = await setup({ server: fakeServer({ fail: 500 }) });
    assert.deepEqual(await down.share.publish(ID), { status: 'failed' });

    // 503 share_unavailable is an answer-only fallback: a conversation never copies a private /answer link.
    const unavailable = await setup({ server: fakeServer({ fail: 503, failBody: { code: 'share_unavailable' } }) });
    assert.deepEqual(await unavailable.share.publish(ID), { status: 'failed' });
});

test('the clipboard write starts before the link exists (Safari keeps the click activation)', async () => {
    const mod = await loadEsmModule('static/js/conversation-share.js', {
        window: { location: { origin: ORIGIN } },
        fetch: fakeServer().fetchImpl,
        navigator: {},
        document: {},
    });
    let handed = null;
    const share = mod.namespace.createConversationShare({
        copy: (link) => { handed = link; return Promise.resolve(true); },
    });
    const pending = share.publish(ID);
    // copy() was given a Promise, synchronously, not a resolved string.
    assert.equal(typeof handed?.then, 'function');
    await pending;
});

test('stop revokes the link and clears the state; a failure reports false', async () => {
    const { share, server } = await setup();
    await share.publish(ID);
    assert.equal(await share.stop(ID), true);
    assert.deepEqual(share.peek(ID), { shared: false });
    assert.deepEqual(server.requests.at(-1), ['DELETE', `/api/conversations/${ID}/share`]);

    const down = await setup({ server: fakeServer({ fail: 500 }) });
    assert.equal(await down.share.stop(ID), false);
});

test('load reads the state without creating anything, and subscribers hear changes', async () => {
    const { share, server } = await setup();
    const heard = [];
    share.subscribe((id, state) => heard.push([id, state.shared]));
    assert.deepEqual(await share.load(ID), { shared: false });
    await share.publish(ID);
    await share.stop(ID);
    assert.deepEqual(server.requests.map(([method]) => method), ['GET', 'POST', 'DELETE']);
    assert.deepEqual(heard, [[ID, false], [ID, true], [ID, false]]);
});
